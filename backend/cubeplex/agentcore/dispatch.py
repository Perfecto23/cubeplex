"""Durable AgentCore dispatch protocol and Postgres state transitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col

from cubeplex.models.agentcore_dispatch import (
    AGENTCORE_DISPATCH_OPERATIONS,
    AGENTCORE_DISPATCH_STATUSES,
    AgentCoreDispatch,
)

DispatchOperation = Literal["prompt", "respond"]
DispatchStatus = Literal["created", "claimed", "finished", "stop_unknown"]


class AgentCoreDispatchError(RuntimeError):
    """A dispatch cannot safely be admitted or executed."""


class DispatchValidationError(AgentCoreDispatchError):
    """A worker payload, scope, session, or native run failed validation."""


class AgentCoreStopUnknown(AgentCoreDispatchError):
    """Native execution cancellation could not prove remote teardown."""


@dataclass(frozen=True, slots=True)
class DispatchScope:
    """Server-owned identity used for database and native-run checks."""

    org_id: str
    workspace_id: str
    conversation_id: str
    user_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class AgentCoreInvocation:
    """The only object sent through the AgentCore Runtime invocation wire."""

    version: Literal[1]
    dispatch_id: UUID

    def model_dump(self) -> dict[str, object]:
        return {"version": self.version, "dispatch_id": str(self.dispatch_id)}


def agentcore_session_id(dispatch_id: UUID | str) -> str:
    """Derive one stable, opaque Runtime session ID from one dispatch UUID.

    The dispatch UUID is the only input.  Scope, prompt text, credentials, and
    user supplied fields never enter the AgentCore session identifier.
    """
    parsed = dispatch_id if isinstance(dispatch_id, UUID) else UUID(str(dispatch_id))
    digest = hashlib.sha256(parsed.bytes).hexdigest()
    return f"cubeplex-agentcore-{digest}"


def invocation_payload(dispatch_id: UUID | str) -> dict[str, object]:
    """Return the strict, credential-free AgentCore invocation payload."""
    parsed = dispatch_id if isinstance(dispatch_id, UUID) else UUID(str(dispatch_id))
    return AgentCoreInvocation(version=1, dispatch_id=parsed).model_dump()


def parse_invocation_payload(payload: Mapping[str, object]) -> AgentCoreInvocation:
    """Validate the worker payload and reject caller supplied scope fields."""
    if set(payload) != {"version", "dispatch_id"}:
        raise DispatchValidationError("agentcore_invocation_shape_invalid")
    if payload.get("version") != 1:
        raise DispatchValidationError("agentcore_invocation_version_invalid")
    dispatch_id = payload.get("dispatch_id")
    if not isinstance(dispatch_id, str):
        raise DispatchValidationError("agentcore_dispatch_id_invalid")
    try:
        parsed = UUID(dispatch_id)
    except ValueError as exc:
        raise DispatchValidationError("agentcore_dispatch_id_invalid") from exc
    return AgentCoreInvocation(version=1, dispatch_id=parsed)


def _context_payload(ctx: Any) -> dict[str, object]:
    """Serialize only the trusted RunContext fields needed by the worker."""
    return {
        "user_id": str(ctx.user_id),
        "org_id": str(ctx.org_id),
        "workspace_id": str(ctx.workspace_id),
        "conversation_id": str(ctx.conversation_id),
        "trigger": str(ctx.trigger),
        "topic_id": ctx.topic_id,
        "is_group_chat": bool(ctx.is_group_chat),
        "sender_display_name": ctx.sender_display_name,
        "sandbox_mode": ctx.sandbox_mode,
        "topic_creator_user_id": ctx.topic_creator_user_id,
        "conversation_creator_user_id": ctx.conversation_creator_user_id,
    }


def build_prompt_request(
    *,
    content: str,
    attachments: list[str],
    ctx: Any,
    model_key: str | None,
    reasoning: Any,
) -> dict[str, object]:
    """Build the immutable prompt request stored in Postgres."""
    return {
        "content": content,
        "attachments": list(attachments),
        "context": _context_payload(ctx),
        "model_key": model_key,
        "reasoning": _reasoning_payload(reasoning),
    }


def build_respond_request(
    *,
    question_id: str,
    answer: Any,
    claim_token: str,
    ctx: Any,
) -> dict[str, object]:
    """Build the immutable HITL response request stored in Postgres."""
    return {
        "question_id": question_id,
        "answer": answer,
        "claim_token": claim_token,
        "context": _context_payload(ctx),
    }


def _reasoning_payload(reasoning: Any) -> dict[str, object]:
    if reasoning is None:
        return {}
    if isinstance(reasoning, Mapping):
        return {str(key): value for key, value in reasoning.items()}
    fields = ("effort", "max_tokens", "temperature", "verbosity")
    return {
        name: getattr(reasoning, name)
        for name in fields
        if getattr(reasoning, name, None) is not None
    }


def _validate_operation(operation: str) -> DispatchOperation:
    if operation not in AGENTCORE_DISPATCH_OPERATIONS:
        raise DispatchValidationError("agentcore_dispatch_operation_invalid")
    return operation  # type: ignore[return-value]


def _validate_status(status: str) -> DispatchStatus:
    if status not in AGENTCORE_DISPATCH_STATUSES:
        raise DispatchValidationError("agentcore_dispatch_status_invalid")
    return status  # type: ignore[return-value]


async def create_dispatch(
    session_maker: async_sessionmaker[Any],
    *,
    scope: DispatchScope,
    operation: DispatchOperation,
    request: dict[str, object],
    claim_token: str | None = None,
) -> AgentCoreDispatch:
    """Insert one immutable dispatch, returning an existing idempotent row."""
    _validate_operation(operation)
    if operation == "respond" and not claim_token:
        raise DispatchValidationError("agentcore_respond_claim_token_required")
    dispatch = AgentCoreDispatch(
        org_id=scope.org_id,
        workspace_id=scope.workspace_id,
        conversation_id=scope.conversation_id,
        user_id=scope.user_id,
        run_id=scope.run_id,
        operation=operation,
        claim_token=claim_token,
        request=json.loads(json.dumps(request, ensure_ascii=False)),
        status="created",
        session_id="",
    )
    dispatch.session_id = agentcore_session_id(dispatch.id)
    async with session_maker() as session:
        session.add(dispatch)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await _find_idempotent_dispatch(
                session,
                run_id=scope.run_id,
                operation=operation,
                claim_token=claim_token,
            )
            if existing is None:
                raise
            _assert_scope(existing, scope)
            return existing
        await session.refresh(dispatch)
        return dispatch


async def _find_idempotent_dispatch(
    session: AsyncSession,
    *,
    run_id: str,
    operation: DispatchOperation,
    claim_token: str | None,
) -> AgentCoreDispatch | None:
    statement = select(AgentCoreDispatch).where(
        col(AgentCoreDispatch.run_id) == run_id,
        col(AgentCoreDispatch.operation) == operation,
    )
    if operation == "respond":
        statement = statement.where(col(AgentCoreDispatch.claim_token) == claim_token)
    return (await session.execute(statement)).scalar_one_or_none()


def _assert_scope(dispatch: AgentCoreDispatch, scope: DispatchScope) -> None:
    if dispatch.request_scope() != (
        scope.org_id,
        scope.workspace_id,
        scope.conversation_id,
        scope.user_id,
        scope.run_id,
    ):
        raise DispatchValidationError("agentcore_dispatch_scope_mismatch")


async def claim_dispatch(
    session_maker: async_sessionmaker[Any],
    *,
    dispatch_id: UUID,
) -> tuple[AgentCoreDispatch, bool]:
    """Atomically claim a created dispatch.

    ``True`` means this caller owns execution.  ``False`` means the dispatch
    was already claimed or terminal, so the caller must never run the agent.
    """
    async with session_maker() as session:
        row = await session.get(AgentCoreDispatch, dispatch_id)
        if row is None:
            raise DispatchValidationError("agentcore_dispatch_not_found")
        if row.session_id != agentcore_session_id(dispatch_id):
            raise DispatchValidationError("agentcore_session_mismatch")
        _validate_operation(row.operation)
        _validate_status(row.status)
        if row.status != "created":
            return row, False
        now = datetime.now(UTC)
        result = await session.execute(
            update(AgentCoreDispatch)
            .where(
                col(AgentCoreDispatch.id) == dispatch_id,
                col(AgentCoreDispatch.status) == "created",
            )
            .values(status="claimed", claimed_at=now, heartbeat_at=now)
            .returning(AgentCoreDispatch)
        )
        claimed = result.scalar_one_or_none()
        if claimed is None:
            await session.rollback()
            latest = await session.get(AgentCoreDispatch, dispatch_id)
            if latest is None:
                raise DispatchValidationError("agentcore_dispatch_not_found")
            return latest, False
        await session.commit()
        return claimed, True


async def mark_dispatch_finished(
    session_maker: async_sessionmaker[Any],
    *,
    dispatch_id: UUID,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Record worker terminal observation without manufacturing run success."""
    async with session_maker() as session:
        row = await session.get(AgentCoreDispatch, dispatch_id)
        if row is None:
            raise DispatchValidationError("agentcore_dispatch_not_found")
        if row.status == "stop_unknown":
            return
        row.status = "finished"
        row.finished_at = datetime.now(UTC)
        row.heartbeat_at = row.finished_at
        row.error_code = error_code
        row.error_message = error_message
        await session.commit()


async def request_dispatch_stop(
    session_maker: async_sessionmaker[Any],
    *,
    run_id: str,
) -> list[AgentCoreDispatch]:
    """Durably fence all active dispatches for a native run."""
    async with session_maker() as session:
        result = await session.execute(
            select(AgentCoreDispatch).where(
                col(AgentCoreDispatch.run_id) == run_id,
                col(AgentCoreDispatch.status).in_(("created", "claimed", "stop_unknown")),
            )
        )
        rows = list(result.scalars().all())
        for row in rows:
            row.stop_requested = True
        await session.commit()
        return rows


async def mark_dispatch_stop_unknown(
    session_maker: async_sessionmaker[Any],
    *,
    dispatch_id: UUID,
    error_message: str | None = None,
) -> None:
    """Keep a stopped-but-unconfirmed dispatch blocking new work."""
    async with session_maker() as session:
        row = await session.get(AgentCoreDispatch, dispatch_id)
        if row is None:
            raise DispatchValidationError("agentcore_dispatch_not_found")
        row.status = "stop_unknown"
        row.stop_requested = True
        row.error_code = "stop_unknown"
        row.error_message = error_message
        row.heartbeat_at = datetime.now(UTC)
        await session.commit()


async def active_dispatch_for_run(
    session_maker: async_sessionmaker[Any],
    *,
    run_id: str,
) -> AgentCoreDispatch | None:
    """Return the durable remote owner that still blocks a native run."""
    async with session_maker() as session:
        result = await session.execute(
            select(AgentCoreDispatch)
            .where(
                col(AgentCoreDispatch.run_id) == run_id,
                col(AgentCoreDispatch.status).in_(("created", "claimed", "stop_unknown")),
            )
            .order_by(col(AgentCoreDispatch.created_at))
        )
        return result.scalars().first()  # type: ignore[no-any-return]


async def active_dispatches(
    session_maker: async_sessionmaker[Any],
) -> list[AgentCoreDispatch]:
    """Return all remote dispatches that still own native execution."""
    async with session_maker() as session:
        result = await session.execute(
            select(AgentCoreDispatch).where(
                col(AgentCoreDispatch.status).in_(("created", "claimed", "stop_unknown"))
            )
        )
        return list(result.scalars().all())


async def latest_dispatch_for_run(
    session_maker: async_sessionmaker[Any],
    *,
    run_id: str,
) -> AgentCoreDispatch | None:
    """Return the newest dispatch, including terminal rows, for idempotency."""
    async with session_maker() as session:
        result = await session.execute(
            select(AgentCoreDispatch)
            .where(col(AgentCoreDispatch.run_id) == run_id)
            .order_by(col(AgentCoreDispatch.created_at).desc())
        )
        return result.scalars().first()  # type: ignore[no-any-return]


async def active_dispatch_for_conversation(
    session_maker: async_sessionmaker[Any],
    *,
    conversation_id: str,
) -> AgentCoreDispatch | None:
    """Return any remote dispatch that still blocks a conversation."""
    async with session_maker() as session:
        result = await session.execute(
            select(AgentCoreDispatch)
            .where(
                col(AgentCoreDispatch.conversation_id) == conversation_id,
                col(AgentCoreDispatch.status).in_(("created", "claimed", "stop_unknown")),
            )
            .order_by(col(AgentCoreDispatch.created_at))
        )
        return result.scalars().first()  # type: ignore[no-any-return]


def request_context_from_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate and return the worker-owned context object from a dispatch."""
    raw = payload.get("context")
    if not isinstance(raw, Mapping):
        raise DispatchValidationError("agentcore_context_missing")
    required = {"user_id", "org_id", "workspace_id", "conversation_id", "trigger"}
    if not required.issubset(raw):
        raise DispatchValidationError("agentcore_context_incomplete")
    return {str(key): value for key, value in raw.items()}
