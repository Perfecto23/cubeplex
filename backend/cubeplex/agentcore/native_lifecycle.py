"""Native task completion and exact-session cancellation in the control plane."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlmodel import col

from cubeplex.models.agentcore_dispatch import AgentCoreDispatch


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
) -> dict[str, Any]:
    from cubeplex.agentcore.native_events import emit_native_terminal
    from cubeplex.agentcore.native_service import NativeTaskError, callback_transaction
    from cubeplex.agents.checkpointer import shared_checkpointer
    from cubeplex.agents.schemas import DoneEvent, ErrorEvent
    from cubeplex.models.agentcore_callback import AgentCoreCallback

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
    ) as tx:
        if tx.replay:
            return tx.result or {}
        dispatch = tx.dispatch
        if dispatch.status == "finished":
            # A new request ID does not permit overwriting an earlier terminal.
            result = {"status": "already_finished", "duplicate": True}
            tx.complete(result)
            return result
        if dispatch.stop_requested and status != "cancelled":
            raise NativeTaskError("native_stop_requested")
        if status == "cancelled" and not dispatch.stop_requested:
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

                # CubeLoop marks completion only after its history writes.
                # Reading a completed-run snapshot proves that durable boundary.
                messages = await cp.snapshot(dispatch.conversation_id, after_run_id=dispatch.run_id)
                current = [m for m in messages if m.run_id == dispatch.run_id]
                final = current[-1] if current else None
                if (
                    not isinstance(final, AssistantMessage)
                    or any(isinstance(block, ToolCall) for block in final.content)
                    or not any(
                        isinstance(block, TextContent) and block.text.strip()
                        for block in final.content
                    )
                ):
                    raise NativeTaskError("native_final_answer_missing")
                if await cp.load_pending_request(dispatch.conversation_id) is not None:
                    raise NativeTaskError("native_pending_question_unresolved")
            elif status == "paused_hitl":
                pending = await cp.load_pending(dispatch.conversation_id)
                if pending is None or pending[1] != dispatch.run_id:
                    raise NativeTaskError("native_pending_question_missing")
            else:
                # A killed VM cannot return a tool result. Repair it from the
                # trusted side only after the stop fence has rejected callbacks.
                from cubeplex.streams.run_manager import _repair_dangling_tool_calls

                await cp.save_pending_request(dispatch.conversation_id, None)
                await _repair_dangling_tool_calls(dispatch.conversation_id)
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
        result = await emit_native_terminal(
            redis,
            prefix=prefix,
            dispatch_id=str(dispatch.id),
            run_id=dispatch.run_id,
            conversation_id=dispatch.conversation_id,
            status=status,
            events=events,
            ttl_seconds=ttl_seconds,
            maxlen=maxlen,
            claim_token=dispatch.claim_token if dispatch.operation == "respond" else None,
        )
        dispatch.status = "finished"
        dispatch.finished_at = datetime.now(UTC)
        dispatch.heartbeat_at = dispatch.finished_at
        dispatch.error_code = error_code if status == "errored" else None
        tx.session.add(dispatch)
        tx.complete(result)
    # These bookkeeping writes follow the durable completion boundary.
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
    )
    return "cancelled"
