"""Task-capability-only control-plane routes for the native MicroVM host."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from cubeplex.agentcore.native_auth import require_capability
from cubeplex.agentcore.native_checkpoint import checkpoint_call
from cubeplex.agentcore.native_events import receive_event
from cubeplex.agentcore.native_model import proxy_model
from cubeplex.agentcore.native_service import (
    NativeTaskError,
    callback_transaction,
    load_native_dispatch,
)
from cubeplex.agentcore.native_storage import load_workspace, present_file, save_workspace
from cubeplex.cache import RedisHandle, redis_dep
from cubeplex.config import config
from cubeplex.db.engine import async_session_maker
from cubeplex.streams.run_events import (
    _active_run_key,
    _run_meta_key,
    get_run_meta,
    touch_run_heartbeat,
)


class SafeNativeRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except HTTPException:
                raise
            except RequestValidationError as exc:
                raise NativeTaskError("native_request_invalid", 422) from exc
            except Exception as exc:
                raise NativeTaskError("native_callback_unavailable", 503) from exc

        return handler


router = APIRouter(
    prefix="/agentcore/tasks/{dispatch_id}",
    tags=["agentcore-native"],
    dependencies=[Depends(require_capability)],
    route_class=SafeNativeRoute,
)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1, le=1)
    request_id: UUID


class CheckpointRequest(Envelope):
    op: str
    args: dict[str, Any]


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1, le=1)
    seq: int = Field(strict=True, ge=1)
    event: dict[str, Any]


class FileBytes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(max_length=1024)
    content_b64: str = Field(max_length=5_592_408)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspaceRequest(Envelope):
    files: list[FileBytes] = Field(max_length=4096)


class PresentRequest(Envelope, FileBytes):
    name: str = Field(min_length=1, max_length=255)
    mime_type: str | None = Field(default=None, max_length=128)


class FinishRequest(Envelope):
    status: Literal["completed", "errored", "paused_hitl", "cancelled"]
    error_code: str | None = Field(default=None, max_length=128, pattern=r"^[a-zA-Z0-9_]+$")


def wrapped(result: Any) -> dict[str, Any]:
    return {"version": 1, "result": result}


def stream_settings() -> dict[str, int]:
    return {
        "ttl_seconds": int(config.get("streaming.run_event_ttl_seconds", 43200)),
        "maxlen": int(config.get("streaming.run_stream_max_events", 1000000)),
    }


@router.post("/claim")
async def claim(
    dispatch_id: UUID, body: Envelope, rds: Annotated[RedisHandle, Depends(redis_dep)]
) -> dict[str, Any]:
    async with callback_transaction(dispatch_id, body.request_id, "claim", {}) as tx:
        if tx.replay:
            # A repeated admission must never grant a second Agent execution,
            # even if the caller lost the first HTTP response.
            return wrapped({**(tx.result or {}), "owner": False})
        d = tx.dispatch
        owner = d.status == "created"
        if owner:
            meta = await get_run_meta(rds.client, prefix=rds.key_prefix, run_id=d.run_id)
            active = await rds.client.get(_active_run_key(rds.key_prefix, d.conversation_id))
            if (
                meta is None
                or meta.conversation_id != d.conversation_id
                or meta.status != "running"
                or active != d.run_id
            ):
                raise NativeTaskError("native_run_claim_conflict")
            if d.operation == "respond":
                from cubeplex.agents.checkpointer import shared_checkpointer

                claim_token = await rds.client.hget(  # type: ignore[misc]
                    _run_meta_key(rds.key_prefix, d.run_id), "claim_token"
                )
                async with shared_checkpointer() as cp:
                    pending = await cp.load_pending(d.conversation_id)
                if (
                    not d.claim_token
                    or claim_token != d.claim_token
                    or pending is None
                    or pending[1] != d.run_id
                    or pending[0].question_id != d.request.get("question_id")
                ):
                    raise NativeTaskError("native_respond_claim_conflict")
            d.status = "claimed"
            d.claimed_at = datetime.now(UTC)
            d.heartbeat_at = d.claimed_at
            tx.session.add(d)
        result = {
            "owner": owner,
            "dispatch_id": str(d.id),
            "run_id": d.run_id,
            "conversation_id": d.conversation_id,
            "session_id": d.session_id,
            "operation": d.operation,
            "content": d.request.get("content"),
            "question_id": d.request.get("question_id"),
            "answer": d.request.get("answer"),
            "system_prompt": d.request["system_prompt"],
            "model": d.request["model"],
            "reasoning": d.request.get("reasoning", {}),
            "stop_requested": d.stop_requested,
        }
        tx.complete(result)
        return wrapped(result)


@router.get("/control")
async def control(
    dispatch_id: UUID, rds: Annotated[RedisHandle, Depends(redis_dep)]
) -> dict[str, Any]:
    async with async_session_maker() as session, session.begin():
        d = await load_native_dispatch(session, dispatch_id, for_update=True)
        if d.status == "created":
            raise NativeTaskError("native_task_not_claimed")
        if d.status == "claimed" and not d.stop_requested:
            now = datetime.now(UTC)
            if d.heartbeat_at is None or (now - d.heartbeat_at).total_seconds() >= 10:
                d.heartbeat_at = now
                session.add(d)
            await touch_run_heartbeat(
                rds.client,
                prefix=rds.key_prefix,
                run_id=d.run_id,
                conversation_id=d.conversation_id,
                ttl_seconds=stream_settings()["ttl_seconds"],
            )
        return wrapped({"status": d.status, "stop_requested": d.stop_requested})


@router.post("/checkpoint")
async def checkpoint(dispatch_id: UUID, body: CheckpointRequest) -> dict[str, Any]:
    return wrapped(await checkpoint_call(dispatch_id, body.request_id, body.op, body.args))


@router.post("/events")
async def events(
    dispatch_id: UUID, body: EventRequest, rds: Annotated[RedisHandle, Depends(redis_dep)]
) -> dict[str, Any]:
    return wrapped(
        await receive_event(
            rds.client,
            prefix=rds.key_prefix,
            dispatch_id=dispatch_id,
            seq=body.seq,
            event=body.event,
            **stream_settings(),
        )
    )


@router.post("/workspace")
async def workspace_put(dispatch_id: UUID, body: WorkspaceRequest) -> dict[str, Any]:
    return wrapped(
        await save_workspace(
            dispatch_id, body.request_id, [file.model_dump() for file in body.files]
        )
    )


@router.get("/workspace")
async def workspace_get(dispatch_id: UUID) -> dict[str, Any]:
    return wrapped(await load_workspace(dispatch_id))


@router.post("/present")
async def present(dispatch_id: UUID, body: PresentRequest) -> dict[str, Any]:
    return wrapped(
        await present_file(
            dispatch_id, body.request_id, body.model_dump(exclude={"version", "request_id"})
        )
    )


@router.post("/model/responses")
async def model_responses(
    dispatch_id: UUID,
    body: dict[str, Any],
    request: Request,
    request_id: Annotated[UUID, Header(alias="X-AgentCore-Request-Id")],
) -> Response:
    text = await proxy_model(
        dispatch_id, request_id, body, encryption_backend=request.app.state.encryption_backend
    )
    return Response(text, media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@router.post("/finish")
async def finish(
    dispatch_id: UUID, body: FinishRequest, rds: Annotated[RedisHandle, Depends(redis_dep)]
) -> dict[str, Any]:
    from cubeplex.agentcore.native_lifecycle import finish_native_dispatch

    return wrapped(
        await finish_native_dispatch(
            rds.client,
            prefix=rds.key_prefix,
            dispatch_id=dispatch_id,
            request_id=body.request_id,
            status=body.status,
            error_code=body.error_code,
            **stream_settings(),
        )
    )
