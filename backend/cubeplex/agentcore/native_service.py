"""Transactional callback guards shared by checkpoints and native lifecycle handlers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from cubeplex.agentcore.dispatch import agentcore_session_id, request_context_from_payload
from cubeplex.cache import RedisHandle
from cubeplex.db.engine import async_session_maker
from cubeplex.models.agentcore_callback import AgentCoreCallback
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch
from cubeplex.models.conversation import Conversation
from cubeplex.models.conversation_execution import ConversationExecutionAdmission
from cubeplex.services.conversation_execution import (
    ConversationExecutionService,
    ExecutionRevokedError,
)
from cubeplex.streams.run_events import run_claim_matches


class NativeTaskError(HTTPException):
    def __init__(self, code: str, status_code: int = 409) -> None:
        super().__init__(status_code=status_code, detail={"code": code})
        self.code = code


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class NativeExecutionBinding:
    admission_id: str
    generation: int
    start_token: str
    attempt_id: str


def native_execution_binding(dispatch: AgentCoreDispatch) -> NativeExecutionBinding | None:
    value = request_context_from_payload(dispatch.request).get("execution")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"admission_id", "generation", "start_token", "attempt_id"}
        or type(value["generation"]) is not int
        or value["generation"] < 0
        or any(
            not isinstance(value[key], str) or not 0 < len(value[key]) <= 64
            for key in ("admission_id", "start_token", "attempt_id")
        )
    ):
        raise NativeTaskError("native_execution_binding_invalid")
    return NativeExecutionBinding(**value)


def native_claim_token(dispatch: AgentCoreDispatch) -> str | None:
    binding = native_execution_binding(dispatch)
    return binding.attempt_id if binding is not None else dispatch.claim_token


async def native_execution_admission(
    session: AsyncSession, dispatch: AgentCoreDispatch
) -> ConversationExecutionAdmission | None:
    binding = native_execution_binding(dispatch)
    if binding is None:
        return None
    admission = await session.get(ConversationExecutionAdmission, binding.admission_id)
    if (
        admission is None
        or admission.org_id != dispatch.org_id
        or admission.workspace_id != dispatch.workspace_id
        or admission.conversation_id != dispatch.conversation_id
        or admission.actor_user_id != dispatch.user_id
        or admission.run_id != dispatch.run_id
        or admission.execution_generation != binding.generation
        or admission.run_start_token != binding.start_token
    ):
        raise NativeTaskError("native_execution_binding_conflict")
    return admission


async def require_native_execution(
    session: AsyncSession,
    dispatch: AgentCoreDispatch,
    *,
    worker_entry: bool = False,
    run_store: RedisHandle | None = None,
) -> None:
    binding = native_execution_binding(dispatch)
    if binding is None:
        return
    await native_execution_admission(session, dispatch)
    service = ConversationExecutionService(
        session, org_id=dispatch.org_id, workspace_id=dispatch.workspace_id
    )
    try:
        if worker_entry and dispatch.operation == "prompt":
            if not await service.record_run_started(
                admission_id=binding.admission_id,
                attempt_id=binding.start_token,
                now=datetime.now(UTC),
            ):
                raise NativeTaskError("native_execution_already_started")
        else:
            await service.require_run_authority(
                admission_id=binding.admission_id, attempt_id=binding.start_token
            )
    except (ExecutionRevokedError, LookupError) as exc:
        raise NativeTaskError("native_execution_revoked") from exc
    admission = await native_execution_admission(session, dispatch)
    if admission is not None and admission.run_terminal_status is not None:
        raise NativeTaskError("native_task_terminal")
    finishing = await session.scalars(
        select(col(AgentCoreCallback.response)).where(
            col(AgentCoreCallback.dispatch_id) == dispatch.id,
            col(AgentCoreCallback.operation) == "finish",
            col(AgentCoreCallback.status) == "pending",
        )
    )
    if any(isinstance(value, dict) and "status" in value for value in finishing):
        raise NativeTaskError("native_task_terminal")
    if run_store is None:
        raise NativeTaskError("native_run_store_unavailable", 503)
    if not await run_claim_matches(
        run_store.client,
        prefix=run_store.key_prefix,
        conversation_id=dispatch.conversation_id,
        run_id=dispatch.run_id,
        claim_token=binding.attempt_id,
    ):
        raise NativeTaskError("native_claim_conflict")


async def load_native_dispatch(
    session: AsyncSession, dispatch_id: UUID, *, for_update: bool = False
) -> AgentCoreDispatch:
    statement = select(AgentCoreDispatch).where(col(AgentCoreDispatch.id) == dispatch_id)
    if for_update:
        statement = statement.with_for_update()
    dispatch = (await session.execute(statement)).scalar_one_or_none()
    if dispatch is None or dispatch.request.get("execution_mode") != "native":
        raise NativeTaskError("native_task_not_found", 404)
    if dispatch.session_id != agentcore_session_id(dispatch.id):
        raise NativeTaskError("native_session_mismatch", 403)
    conversation = await session.get(Conversation, dispatch.conversation_id)
    if (
        conversation is None
        or conversation.org_id != dispatch.org_id
        or conversation.workspace_id != dispatch.workspace_id
    ):
        raise NativeTaskError("native_scope_mismatch", 403)
    raw = request_context_from_payload(dispatch.request)
    expected = {
        "user_id": dispatch.user_id,
        "org_id": dispatch.org_id,
        "workspace_id": dispatch.workspace_id,
        "conversation_id": dispatch.conversation_id,
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise NativeTaskError("native_scope_mismatch", 403)
    return dispatch


@dataclass
class CallbackTransaction:
    session: AsyncSession
    dispatch: AgentCoreDispatch
    receipt: AgentCoreCallback
    replay: bool
    result: dict[str, Any] | None

    def complete(self, result: dict[str, Any]) -> None:
        if self.replay:
            return
        self.receipt.status = "done"
        self.receipt.response = result
        self.receipt.completed_at = datetime.now(UTC)
        self.session.add(self.receipt)
        self.result = result


@asynccontextmanager
async def callback_transaction(
    dispatch_id: UUID,
    request_id: UUID,
    operation: str,
    payload: dict[str, Any],
    *,
    allow_stopped: bool = False,
    allow_terminal: bool = False,
    resume_pending: bool = False,
    run_store: RedisHandle | None = None,
) -> AsyncIterator[CallbackTransaction]:
    """One SQL transaction owns dispatch lock, mutation and durable receipt.

    Already committed receipts are readable after stop/finish. A pending
    external-operation receipt never grants a second invocation.
    """
    fingerprint = payload_hash({"operation": operation, "payload": payload})
    async with async_session_maker() as session, session.begin():
        dispatch = await load_native_dispatch(session, dispatch_id, for_update=True)
        receipt = await session.get(AgentCoreCallback, (dispatch_id, request_id))
        if receipt is not None:
            if receipt.payload_sha256 != fingerprint or receipt.operation != operation:
                raise NativeTaskError("native_request_conflict")
            if receipt.status != "done" and not resume_pending:
                raise NativeTaskError("native_callback_outcome_unknown")
            yield CallbackTransaction(
                session, dispatch, receipt, receipt.status == "done", receipt.response
            )
            return
        if dispatch.stop_requested and not allow_stopped:
            raise NativeTaskError("native_task_stopped")
        if dispatch.status in {"finished", "stop_unknown"} and not allow_terminal:
            raise NativeTaskError("native_task_terminal")
        if operation not in {"claim", "finish"} and dispatch.status == "created":
            raise NativeTaskError("native_task_not_claimed")
        if operation not in {"claim", "finish"}:
            await require_native_execution(session, dispatch, run_store=run_store)
        receipt = AgentCoreCallback(
            dispatch_id=dispatch_id,
            request_id=request_id,
            operation=operation,
            payload_sha256=fingerprint,
        )
        session.add(receipt)
        yield CallbackTransaction(session, dispatch, receipt, False, None)
