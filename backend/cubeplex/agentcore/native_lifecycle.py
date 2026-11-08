"""Native task completion and exact-session cancellation in the control plane."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlmodel import col

from cubeplex.agentcore.native_service import CallbackTransaction
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch


async def _projection_expired(redis: Redis, prefix: str, dispatch: AgentCoreDispatch) -> bool:
    from cubeplex.streams.run_events import _active_run_key, _run_meta_key

    return bool(
        await redis.eval(  # type: ignore[misc]
            "return redis.call('EXISTS', KEYS[1]) == 0 "
            "and redis.call('GET', KEYS[2]) ~= ARGV[1] and 1 or 0",
            2,
            _run_meta_key(prefix, dispatch.run_id),
            _active_run_key(prefix, dispatch.conversation_id),
            dispatch.run_id,
        )
    )


async def _prepare_native_finish(
    tx: CallbackTransaction,
    *,
    status: str,
    error_code: str | None,
    teardown_confirmed: bool = False,
) -> dict[str, Any]:
    from cubeloop.checkpointer.postgres.models import CubeloopRun

    from cubeplex.agentcore.native_service import (
        NativeTaskError,
        native_execution_admission,
        native_execution_binding,
    )
    from cubeplex.agents.checkpointer import shared_checkpointer
    from cubeplex.agents.schemas import DoneEvent, ErrorEvent
    from cubeplex.models.agentcore_callback import AgentCoreCallback
    from cubeplex.services.conversation_execution import (
        ConversationExecutionService,
        RunTerminalStatus,
    )

    dispatch = tx.dispatch
    binding = native_execution_binding(dispatch)
    admission = await native_execution_admission(tx.session, dispatch)
    stopped = dispatch.stop_requested or bool(
        admission is not None
        and (admission.run_stop_requested_at is not None or admission.revoked_at is not None)
    )
    if stopped and status == "errored":
        status, error_code = "cancelled", None
    if stopped and status != "cancelled":
        raise NativeTaskError("native_stop_requested")
    if status == "cancelled" and not stopped:
        raise NativeTaskError("native_cancel_not_requested")
    if status in {"completed", "paused_hitl"}:
        saved = await tx.session.scalar(
            select(col(AgentCoreCallback.request_id))
            .where(
                col(AgentCoreCallback.dispatch_id) == dispatch.id,
                col(AgentCoreCallback.operation) == "workspace",
                col(AgentCoreCallback.status) == "done",
            )
            .limit(1)
        )
        if saved is None:
            raise NativeTaskError("native_workspace_not_saved")
    async with shared_checkpointer() as cp:
        if status == "completed":
            from cubeloop.providers.base import AssistantMessage, TextContent, ToolCall

            messages = await cp.snapshot(dispatch.conversation_id, after_run_id=dispatch.run_id)
            current = [m for m in messages if m.run_id == dispatch.run_id]
            final = current[-1] if current else None
            if (
                not isinstance(final, AssistantMessage)
                or any(isinstance(block, ToolCall) for block in final.content)
                or not any(
                    isinstance(block, TextContent) and block.text.strip() for block in final.content
                )
            ):
                raise NativeTaskError("native_final_answer_missing")
            pending = await cp.load_pending(dispatch.conversation_id)
            if (
                pending is not None
                and dispatch.operation == "respond"
                and pending[1] == dispatch.run_id
                and pending[0].question_id == dispatch.request.get("question_id")
            ):
                await cp.clear_pending_request_if_matches(
                    dispatch.conversation_id,
                    question_id=pending[0].question_id,
                    run_id=dispatch.run_id,
                )
                pending = await cp.load_pending(dispatch.conversation_id)
            if pending is not None:
                raise NativeTaskError("native_pending_question_unresolved")
        elif status == "paused_hitl":
            pending = await cp.load_pending(dispatch.conversation_id)
            if pending is None or pending[1] != dispatch.run_id:
                raise NativeTaskError("native_pending_question_missing")
        else:
            from cubeplex.streams.run_manager import _repair_dangling_tool_calls

            pending = await cp.load_pending(dispatch.conversation_id)
            if pending is not None and pending[1] != dispatch.run_id:
                if not teardown_confirmed:
                    raise NativeTaskError("native_pending_scope_conflict")
                pending = None
            await _repair_dangling_tool_calls(
                dispatch.conversation_id, expected_run_id=dispatch.run_id
            )
            if pending is not None:
                if not await cp.clear_pending_request_if_matches(
                    dispatch.conversation_id,
                    question_id=pending[0].question_id,
                    run_id=dispatch.run_id,
                ):
                    raise NativeTaskError("native_pending_scope_conflict")
            run = await tx.session.get(CubeloopRun, (dispatch.conversation_id, dispatch.run_id))
            if run is not None:
                await cp.mark_run_complete(dispatch.conversation_id, dispatch.run_id)
    worker_started = admission is not None and admission.run_started_at is not None
    if binding is not None and status != "paused_hitl" and worker_started:
        service = ConversationExecutionService(
            tx.session, org_id=dispatch.org_id, workspace_id=dispatch.workspace_id
        )
        if not await service.record_run_terminal_outcome(
            admission_id=binding.admission_id,
            attempt_id=binding.start_token,
            status=cast(RunTerminalStatus, status),
            now=datetime.now(UTC),
        ):
            raise NativeTaskError("native_terminal_outcome_conflict")
    now = datetime.now(UTC).isoformat()
    events: list[dict[str, Any]] = []
    if status == "errored":
        events.append(
            ErrorEvent(
                timestamp=now,
                data={
                    "error_code": error_code or "native_execution_failed",
                    "message": "The remote agent could not finish this task.",
                },
            ).model_dump()
        )
    done: dict[str, Any] = {}
    if status == "paused_hitl":
        done["paused"] = True
    if status == "cancelled":
        done["cancelled"] = True
    events.append(DoneEvent(timestamp=now, data=done).model_dump())
    return {
        "status": status,
        "error_code": error_code,
        "events": events,
        "worker_started": worker_started,
    }


async def finish_native_dispatch(
    redis: Redis,
    *,
    prefix: str,
    dispatch_id: UUID,
    request_id: UUID,
    status: str,
    error_code: str | None,
    ttl_seconds: int,
    maxlen: int,
    teardown_confirmed: bool = False,
) -> dict[str, Any]:
    from cubeplex.agentcore.native_events import emit_native_terminal
    from cubeplex.agentcore.native_service import (
        NativeTaskError,
        callback_transaction,
        native_claim_token,
        native_execution_admission,
        native_execution_binding,
        require_native_execution,
    )
    from cubeplex.cache import RedisHandle
    from cubeplex.config import config
    from cubeplex.models.agentcore_callback import AgentCoreCallback
    from cubeplex.services.conversation_execution import ConversationExecutionService
    from cubeplex.streams.hitl_resume import begin_resume_finalization
    from cubeplex.streams.run_events import clear_active_run, expire_run_data

    if status not in {"completed", "errored", "paused_hitl", "cancelled"}:
        raise NativeTaskError("native_terminal_status_invalid")
    payload = {"status": status, "error_code": error_code}
    async with callback_transaction(
        dispatch_id,
        request_id,
        "finish",
        payload,
        allow_stopped=True,
        allow_terminal=True,
        resume_pending=True,
    ) as tx:
        if tx.replay:
            return tx.result or {}
        dispatch = tx.dispatch
        if dispatch.status == "finished":
            result = {"status": "already_finished", "duplicate": True}
            tx.complete(result)
            return result
        binding = native_execution_binding(dispatch)
        token = native_claim_token(dispatch)
        prepared = tx.receipt.response
        if prepared is None:
            prior = await tx.session.scalar(
                select(AgentCoreCallback)
                .where(
                    col(AgentCoreCallback.dispatch_id) == dispatch_id,
                    col(AgentCoreCallback.operation) == "finish",
                    col(AgentCoreCallback.status) == "pending",
                    col(AgentCoreCallback.request_id) != request_id,
                )
                .order_by(col(AgentCoreCallback.created_at))
                .limit(1)
            )
            if prior is not None and prior.response is not None:
                # A durable terminal decision wins over later stop/error reports.
                prepared = dict(prior.response)
            else:
                if status in {"completed", "paused_hitl"}:
                    await require_native_execution(
                        tx.session, dispatch, run_store=RedisHandle(redis, prefix)
                    )
                expired_after_teardown = (
                    teardown_confirmed
                    and status == "cancelled"
                    and dispatch.stop_requested
                    and await _projection_expired(redis, prefix, dispatch)
                )
                if (
                    binding is not None
                    and not expired_after_teardown
                    and not await begin_resume_finalization(
                        redis,
                        prefix=prefix,
                        conversation_id=dispatch.conversation_id,
                        run_id=dispatch.run_id,
                        claim_token=binding.attempt_id,
                        ttl_seconds=ttl_seconds,
                        lease_seconds=max(
                            1, int(config.get("lifecycle.stale_run_threshold_seconds", 180))
                        ),
                    )
                ):
                    raise NativeTaskError("native_claim_conflict")
                prepared = await _prepare_native_finish(
                    tx,
                    status=status,
                    error_code=error_code,
                    teardown_confirmed=expired_after_teardown,
                )
            prepared = {
                **prepared,
                "request_status": status,
                "request_error_code": error_code,
            }
            tx.receipt.response = prepared
            tx.session.add(tx.receipt)
    # Commit the terminal proof before publishing or releasing the Redis slot.
    projection_expired = await _projection_expired(redis, prefix, dispatch)
    if projection_expired:
        # The prepared receipt already committed the checkpoint and outcome.
        # Expired delivery state needs only the durable cleanup receipt, never a new slot.
        result = {
            "status": prepared["status"],
            "applied": False,
            "event_ids": [],
            "recovered": True,
        }
    else:
        result = await emit_native_terminal(
            redis,
            prefix=prefix,
            dispatch_id=str(dispatch_id),
            run_id=dispatch.run_id,
            conversation_id=dispatch.conversation_id,
            status=prepared["status"],
            events=prepared["events"],
            ttl_seconds=ttl_seconds,
            maxlen=maxlen,
            claim_token=token,
            release_active=False,
        )
    if prepared["status"] != "paused_hitl" and not projection_expired:
        released = await clear_active_run(
            redis,
            prefix=prefix,
            conversation_id=dispatch.conversation_id,
            run_id=dispatch.run_id,
            claim_token=token,
        )
        expired = await expire_run_data(
            redis,
            prefix=prefix,
            run_id=dispatch.run_id,
            ttl_seconds=ttl_seconds,
            claim_token=token,
        )
        if not released or not expired:
            raise NativeTaskError("native_claim_conflict")
    async with callback_transaction(
        dispatch_id,
        request_id,
        "finish",
        payload,
        allow_stopped=True,
        allow_terminal=True,
        resume_pending=True,
    ) as tx:
        if tx.replay:
            return tx.result or {}
        dispatch = tx.dispatch
        binding = native_execution_binding(dispatch)
        if binding is not None and prepared["status"] != "paused_hitl":
            service = ConversationExecutionService(
                tx.session, org_id=dispatch.org_id, workspace_id=dispatch.workspace_id
            )
            finished = await service.record_run_finished(
                admission_id=binding.admission_id,
                attempt_id=binding.start_token,
                worker_started=prepared["worker_started"],
                now=datetime.now(UTC),
            )
            admission = await native_execution_admission(tx.session, dispatch)
            if not finished and (admission is None or admission.run_finished_at is None):
                raise NativeTaskError("native_finish_receipt_conflict")
        dispatch.status = "finished"
        dispatch.finished_at = datetime.now(UTC)
        dispatch.heartbeat_at = dispatch.finished_at
        dispatch.error_code = prepared["error_code"] if prepared["status"] == "errored" else None
        tx.session.add(dispatch)
        tx.complete(result)
    from cubeplex.api.routes.v1.conversations import (
        _enqueue_search_index,
        _update_conversation_timestamp,
    )

    with suppress(Exception):
        await _update_conversation_timestamp(
            dispatch.conversation_id,
            org_id=dispatch.org_id,
            workspace_id=dispatch.workspace_id,
            user_id=dispatch.user_id,
        )
        await _enqueue_search_index(
            dispatch.conversation_id,
            org_id=dispatch.org_id,
            workspace_id=dispatch.workspace_id,
            user_id=dispatch.user_id,
        )
    return result


async def reconcile_native_finish(
    redis: Redis,
    *,
    prefix: str,
    dispatch_id: UUID,
    ttl_seconds: int,
    maxlen: int,
) -> bool:
    """Resume committed finish preparation without invoking the native worker."""
    from cubeplex.db.engine import async_session_maker
    from cubeplex.models.agentcore_callback import AgentCoreCallback

    async with async_session_maker() as session:
        receipts = await session.scalars(
            select(AgentCoreCallback)
            .where(
                col(AgentCoreCallback.dispatch_id) == dispatch_id,
                col(AgentCoreCallback.operation) == "finish",
                col(AgentCoreCallback.status) == "pending",
            )
            .order_by(col(AgentCoreCallback.created_at))
        )
        receipt = next(
            (
                row
                for row in receipts
                if isinstance(row.response, dict) and "status" in row.response
            ),
            None,
        )
        if receipt is None:
            return False
        prepared = receipt.response
        assert prepared is not None
        request_id = receipt.request_id
    await finish_native_dispatch(
        redis,
        prefix=prefix,
        dispatch_id=dispatch_id,
        request_id=request_id,
        status=prepared["request_status"],
        error_code=prepared["request_error_code"],
        ttl_seconds=ttl_seconds,
        maxlen=maxlen,
    )
    return True


async def cancel_native_dispatch(
    dispatch: AgentCoreDispatch,
    *,
    client: Any,
    redis: Redis,
    prefix: str,
    ttl_seconds: int,
    maxlen: int,
) -> str:
    from cubeplex.agentcore.dispatch import (
        active_dispatch_for_run,
        mark_dispatch_stop_unknown,
        request_dispatch_stop,
    )
    from cubeplex.db.engine import async_session_maker

    # This also fences a late Runtime invocation which has not claimed yet.
    await request_dispatch_stop(async_session_maker, run_id=dispatch.run_id)
    confirmed = False
    try:
        result = await asyncio.wait_for(client.stop(dispatch.id), timeout=15)
        confirmed = result.get("statusCode") == 200
    except Exception as exc:
        response = getattr(exc, "response", {})
        confirmed = response.get("Error", {}).get("Code") == "ResourceNotFoundException"
    current = await active_dispatch_for_run(async_session_maker, run_id=dispatch.run_id)
    if current is None:
        return "cancelled"
    if not confirmed:
        await mark_dispatch_stop_unknown(
            async_session_maker,
            dispatch_id=dispatch.id,
            error_message="Native Runtime session teardown could not be confirmed.",
        )
        return "stop_unknown"
    await finish_native_dispatch(
        redis,
        prefix=prefix,
        dispatch_id=dispatch.id,
        request_id=uuid4(),
        status="cancelled",
        error_code=None,
        ttl_seconds=ttl_seconds,
        maxlen=maxlen,
        teardown_confirmed=True,
    )
    return "cancelled"
