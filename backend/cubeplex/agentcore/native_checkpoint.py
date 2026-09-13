"""CubeLoop checkpoint RPCs using its existing tables and one receipt transaction."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import msgpack
from cubeloop.checkpointer.postgres.models import (
    CubeloopHitlAnswer,
    CubeloopMessage,
    CubeloopRun,
    CubeloopThread,
)
from cubeloop.hitl.types import HitlRequest
from cubeloop.providers.base import Message
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, func, null, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cubeplex.agentcore.native_service import (
    NativeTaskError,
    callback_transaction,
    canonical,
    load_native_dispatch,
)
from cubeplex.db.engine import async_session_maker
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch

ARGUMENTS = {
    "load": set(),
    "append": {"messages"},
    "save_extra": {"extra"},
    "save_pending_request": {"request"},
    "load_pending_request": set(),
    "load_pending": set(),
    "claim_run": set(),
    "mark_run_complete": set(),
    "save_hitl_answer": {"question_id", "answer"},
    "load_hitl_answer": {"question_id"},
    "clear_hitl_answers": {"question_ids"},
}
READS = {"load", "load_pending_request", "load_pending", "load_hitl_answer"}


def validate_arguments(operation: str, args: dict[str, Any]) -> None:
    if operation not in ARGUMENTS or set(args) != ARGUMENTS[operation]:
        raise NativeTaskError("native_checkpoint_operation_denied", 422)
    if len(canonical(args)) > 2 * 1024 * 1024:
        raise NativeTaskError("native_checkpoint_too_large", 413)


def _message(row: CubeloopMessage) -> dict[str, Any]:
    try:
        message: Message = TypeAdapter(Message).validate_python(
            msgpack.unpackb(row.payload, raw=False)
        )
        message.metadata = row.msg_metadata
        return message.model_dump(mode="json")
    except (ValueError, ValidationError, msgpack.UnpackException) as exc:
        raise NativeTaskError("native_checkpoint_corrupt", 500) from exc


async def _read(
    session: AsyncSession, dispatch: AgentCoreDispatch, operation: str, args: dict[str, Any]
) -> Any:
    thread_id, run_id = dispatch.conversation_id, dispatch.run_id
    thread = await session.get(CubeloopThread, thread_id)
    if operation == "load":
        rows = (
            (
                await session.execute(
                    select(CubeloopMessage)
                    .where(CubeloopMessage.thread_id == thread_id)
                    .order_by(CubeloopMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        if thread is None and not rows:
            return None
        return {
            "messages": [_message(row) for row in rows],
            "extra": thread.extra if thread else {},
            "parent_thread_id": thread.parent_thread_id if thread else None,
        }
    if operation == "load_hitl_answer":
        question_id = args["question_id"]
        if not isinstance(question_id, str):
            raise NativeTaskError("native_question_invalid", 422)
        answer = await session.get(CubeloopHitlAnswer, (thread_id, run_id, question_id))
        return answer.answer if answer else None
    if thread is None or thread.pending_request is None:
        return None
    if thread.run_id != run_id:
        raise NativeTaskError("native_pending_scope_conflict")
    if operation == "load_pending":
        return {"request": thread.pending_request, "run_id": thread.run_id}
    return thread.pending_request


async def _mutate(
    session: AsyncSession, dispatch: AgentCoreDispatch, operation: str, args: dict[str, Any]
) -> None:
    thread_id, run_id = dispatch.conversation_id, dispatch.run_id
    # Same advisory lock and storage representation as CubeLoop 0.14.1.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:thread))"), {"thread": thread_id}
    )
    await session.execute(
        insert(CubeloopThread).values(thread_id=thread_id).on_conflict_do_nothing()
    )
    thread = await session.get(CubeloopThread, thread_id)
    assert thread is not None
    run = await session.get(CubeloopRun, (thread_id, run_id))
    if operation == "claim_run":
        if run is not None:
            code = "native_run_completed" if run.completed_at else "native_run_already_claimed"
            raise NativeTaskError(code)
        session.add(CubeloopRun(thread_id=thread_id, run_id=run_id))
        return
    if operation == "append":
        if run is None or run.completed_at is not None:
            raise NativeTaskError("native_checkpoint_run_not_writable")
        try:
            messages = TypeAdapter(list[Message]).validate_python(args["messages"])
        except ValidationError as exc:
            raise NativeTaskError("native_messages_invalid", 422) from exc
        last = await session.scalar(
            select(func.coalesce(func.max(CubeloopMessage.seq), 0)).where(
                CubeloopMessage.thread_id == thread_id
            )
        )
        for index, supplied in enumerate(messages):
            message = supplied.model_copy(update={"run_id": run_id})
            session.add(
                CubeloopMessage(
                    thread_id=thread_id,
                    seq=int(last or 0) + index + 1,
                    role="tool" if message.role == "tool_result" else message.role,
                    msg_metadata=message.metadata,
                    payload=msgpack.packb(message.model_dump(mode="json"), use_bin_type=True),
                    run_id=run_id,
                )
            )
        return
    if operation == "mark_run_complete":
        if run is None:
            raise NativeTaskError("native_run_not_claimed")
        if run.completed_at is None:
            highest = await session.scalar(
                select(func.coalesce(func.max(CubeloopRun.completion_seq), 0)).where(
                    CubeloopRun.thread_id == thread_id
                )
            )
            run.completed_at, run.completion_seq = datetime.now(UTC), int(highest or 0) + 1
        return
    if operation == "save_extra":
        if not isinstance(args["extra"], dict):
            raise NativeTaskError("native_extra_invalid", 422)
        thread.extra = {**thread.extra, **args["extra"]}
        thread.updated_at = datetime.now(UTC)
        return
    if operation == "save_pending_request":
        supplied = args["request"]
        if thread.pending_request is not None and thread.run_id != run_id:
            raise NativeTaskError("native_pending_scope_conflict")
        if supplied is None:
            # SQLAlchemy JSONB otherwise turns Python None into JSON null;
            # CubeLoop's asyncpg reader requires SQL NULL for cleared state.
            await session.execute(
                update(CubeloopThread)
                .where(CubeloopThread.thread_id == thread_id)
                .values(pending_request=null(), run_id=None, updated_at=datetime.now(UTC))
            )
            return
        else:
            try:
                request = HitlRequest.model_validate(supplied)
            except ValidationError as exc:
                raise NativeTaskError("native_pending_invalid", 422) from exc
            thread.pending_request = request.model_dump(mode="json")
            thread.run_id = run_id
        thread.updated_at = datetime.now(UTC)
        return
    if operation == "save_hitl_answer":
        # The VM can persist the authorized answer for CubeLoop replay; it
        # cannot fabricate a decision or approve its own question.
        if (
            dispatch.operation != "respond"
            or args["question_id"] != dispatch.request.get("question_id")
            or args["answer"] != dispatch.request.get("answer")
        ):
            raise NativeTaskError("native_answer_denied", 403)
        await session.execute(
            insert(CubeloopHitlAnswer)
            .values(
                thread_id=thread_id,
                run_id=run_id,
                question_id=dispatch.request["question_id"],
                answer=dispatch.request["answer"],
            )
            .on_conflict_do_update(
                index_elements=["thread_id", "run_id", "question_id"],
                set_={"answer": dispatch.request["answer"], "answered_at": datetime.now(UTC)},
            )
        )
        return
    question_ids = args["question_ids"]
    if question_ids is not None and (
        not isinstance(question_ids, list)
        or any(not isinstance(item, str) for item in question_ids)
    ):
        raise NativeTaskError("native_question_invalid", 422)
    statement = delete(CubeloopHitlAnswer).where(
        CubeloopHitlAnswer.thread_id == thread_id, CubeloopHitlAnswer.run_id == run_id
    )
    if question_ids is not None:
        statement = statement.where(CubeloopHitlAnswer.question_id.in_(question_ids))
    await session.execute(statement)


async def checkpoint_call(
    dispatch_id: UUID, request_id: UUID, operation: str, args: dict[str, Any]
) -> Any:
    validate_arguments(operation, args)
    if operation in READS:
        async with async_session_maker() as session:
            dispatch = await load_native_dispatch(session, dispatch_id)
            if dispatch.status == "created":
                raise NativeTaskError("native_task_not_claimed")
            return await _read(session, dispatch, operation, args)
    async with callback_transaction(
        dispatch_id, request_id, f"checkpoint:{operation}", args
    ) as callback:
        if callback.replay:
            return (callback.result or {}).get("value")
        await _mutate(callback.session, callback.dispatch, operation, args)
        callback.complete({"value": None})
        return None
