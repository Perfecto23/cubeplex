"""Real Postgres, Redis and ObjectStore callback transaction invariants."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, select

from cubeplex.agentcore import native_auth, native_model
from cubeplex.agentcore.native_checkpoint import checkpoint_call
from cubeplex.agentcore.native_router import router
from cubeplex.agentcore.native_service import NativeTaskError, callback_transaction
from cubeplex.agentcore.native_storage import load_workspace, present_file, save_workspace
from cubeplex.agents.checkpointer import shared_checkpointer
from cubeplex.db.engine import async_session_maker
from cubeplex.models.agentcore_callback import AgentCoreCallback
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch
from cubeplex.models.presented_file import PresentedFile
from cubeplex.objectstore import get_objectstore_client
from cubeplex.streams.run_events import _active_run_key, _run_events_key, _run_meta_key
from tests.e2e.test_agentcore_native_lifecycle import Task, native_task  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def cp_task(native_task: Task) -> AsyncIterator[Task]:  # noqa: F811
    yield native_task
    _, _, d = native_task
    store = get_objectstore_client()
    async with async_session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(PresentedFile).where(PresentedFile.conversation_id == d.conversation_id)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await store.delete_file(row.object_key)
        saved = (
            (
                await session.execute(
                    select(AgentCoreCallback).where(
                        AgentCoreCallback.dispatch_id == d.id,
                        AgentCoreCallback.operation == "workspace",
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in saved:
            if row.response and "object_key" in row.response:
                await store.delete_file(row.response["object_key"])
        await session.execute(
            delete(PresentedFile).where(PresentedFile.conversation_id == d.conversation_id)
        )
        await session.commit()


def message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def file_bytes(text: str, path: str = "result.txt") -> dict[str, Any]:
    raw = text.encode()
    return {
        "path": path,
        "content_b64": base64.b64encode(raw).decode(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


async def test_checkpoint_commit_retry_stop_and_public_checkpointer_parity(cp_task: Task) -> None:
    _, _, d = cp_task
    await checkpoint_call(d.id, uuid4(), "claim_run", {})
    req = uuid4()
    args = {"messages": [message("once")]}
    await asyncio.gather(*(checkpoint_call(d.id, req, "append", args) for _ in range(6)))
    async with shared_checkpointer() as cp:
        history = await cp.load(d.conversation_id)
    assert history is not None and len(history.messages) == 1
    assert history.messages[0].run_id == d.run_id
    with pytest.raises(NativeTaskError) as conflict:
        await checkpoint_call(d.id, req, "append", {"messages": [message("different")]})
    assert conflict.value.code == "native_request_conflict"
    async with async_session_maker() as session:
        row = await session.get(AgentCoreDispatch, d.id)
        assert row is not None
        row.stop_requested = True
        await session.commit()
    # Commit happened before stop, so its exact receipt is still readable.
    assert await checkpoint_call(d.id, req, "append", args) is None
    with pytest.raises(NativeTaskError) as stopped:
        await checkpoint_call(d.id, uuid4(), "append", args)
    assert stopped.value.code == "native_task_stopped"


async def test_checkpoint_error_rolls_back_message_and_receipt_together(cp_task: Task) -> None:
    from cubeplex.agentcore.native_checkpoint import _mutate

    _, _, d = cp_task
    await checkpoint_call(d.id, uuid4(), "claim_run", {})
    req, args = uuid4(), {"messages": [message("rollback")]}
    with pytest.raises(RuntimeError, match="interrupt"):
        async with callback_transaction(d.id, req, "checkpoint:append", args) as tx:
            await _mutate(tx.session, tx.dispatch, "append", args)
            raise RuntimeError("interrupt")
    async with async_session_maker() as session:
        assert await session.get(AgentCoreCallback, (d.id, req)) is None
    await checkpoint_call(d.id, req, "append", args)
    history = await checkpoint_call(d.id, uuid4(), "load", {})
    assert len(history["messages"]) == 1


async def test_tool_result_storage_matches_official_postgres_checkpoint(cp_task: Task) -> None:
    from cubeloop.providers.base import AssistantMessage, TextContent, ToolCall, ToolResultMessage

    _, _, d = cp_task
    await checkpoint_call(d.id, uuid4(), "claim_run", {})
    messages = [
        message("use tool"),
        AssistantMessage(content=[ToolCall(id="call-1", name="execute", arguments={})]).model_dump(
            mode="json"
        ),
        ToolResultMessage(
            tool_call_id="call-1", tool_name="execute", content=[TextContent(text="ok")]
        ).model_dump(mode="json"),
        AssistantMessage(content=[TextContent(text="done")]).model_dump(mode="json"),
    ]
    await checkpoint_call(d.id, uuid4(), "append", {"messages": messages})
    await checkpoint_call(d.id, uuid4(), "mark_run_complete", {})
    async with shared_checkpointer() as cp:
        loaded = await cp.load(d.conversation_id)
        assert loaded is not None
        snapshot = await cp.snapshot(d.conversation_id, after_run_id=d.run_id)
    assert len(loaded.messages) == len(snapshot) == 4
    assert isinstance(loaded.messages[2], ToolResultMessage)
    assert loaded.messages[2].tool_call_id == "call-1"
    assert snapshot[2].role == "tool_result"


async def test_vm_cannot_self_answer_or_choose_checkpoint_scope(cp_task: Task) -> None:
    _, _, d = cp_task
    with pytest.raises(NativeTaskError) as denied:
        await checkpoint_call(
            d.id, uuid4(), "save_hitl_answer", {"question_id": "q", "answer": {"approved": True}}
        )
    assert denied.value.code == "native_answer_denied"
    with pytest.raises(NativeTaskError):
        await checkpoint_call(d.id, uuid4(), "load", {"thread_id": "other-thread"})


async def test_workspace_and_present_survive_using_existing_objectstore(cp_task: Task) -> None:
    _, _, d = cp_task
    req = uuid4()
    file = file_bytes("durable artifact")
    first = await save_workspace(d.id, req, [file])
    assert await save_workspace(d.id, req, [file]) == first
    assert (await load_workspace(d.id))["files"] == [file]
    present_req = uuid4()
    payload = {**file, "name": "Result", "mime_type": "text/plain"}
    result = await present_file(d.id, present_req, payload)
    assert await present_file(d.id, present_req, payload) == result
    assert result["conversation_id"] == d.conversation_id and result["run_id"] == d.run_id
    async with async_session_maker() as session:
        row = await session.get(PresentedFile, result["id"])
        assert row is not None
        raw, _ = await get_objectstore_client().download_file(row.object_key)
        assert raw == b"durable artifact"
    for path in ("../escape", ".git/config", "credentials.json", "sub/../escape"):
        with pytest.raises(NativeTaskError):
            await save_workspace(d.id, uuid4(), [file_bytes("no", path)])


async def test_router_claim_auth_event_dedup_and_control_heartbeat(
    cp_task: Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis, prefix, d = cp_task
    async with async_session_maker() as session:
        row = await session.get(AgentCoreDispatch, d.id)
        assert row is not None
        row.status = "created"
        row.request = {
            **row.request,
            "model": {"id": "fixture", "max_output_tokens": 2048},
            "system_prompt": "test",
        }
        await session.commit()
    monkeypatch.setattr(
        native_auth,
        "config",
        SimpleNamespace(
            get=lambda key, default=None: (
                "test-only-strong-secret-1234567890123456789012345"
                if key == "auth.jwt_secret"
                else default
            )
        ),
    )
    app = FastAPI()
    app.state.redis, app.state.redis_key_prefix = redis, prefix
    app.include_router(router, prefix="/api/v1")
    base = f"/api/v1/agentcore/tasks/{d.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get(base + "/control")).status_code == 401
        headers = {"Authorization": "Bearer " + native_auth.issue_capability(d.id)}
        req = {"version": 1, "request_id": str(uuid4())}
        assert (
            await client.post(base + "/claim", headers=headers, json={**req, "version": True})
        ).status_code == 422
        assert (
            await client.post(
                base + "/checkpoint", headers=headers, json={**req, "op": "load", "args": {}}
            )
        ).status_code == 409
        await redis.delete(_active_run_key(prefix, d.conversation_id))
        rejected = await client.post(base + "/claim", headers=headers, json=req)
        assert rejected.status_code == 409
        async with async_session_maker() as session:
            unchanged = await session.get(AgentCoreDispatch, d.id)
            assert unchanged is not None and unchanged.status == "created"
        await redis.set(_active_run_key(prefix, d.conversation_id), d.run_id, ex=120)
        claimed = await client.post(base + "/claim", headers=headers, json=req)
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["result"]["owner"] is True
        assert (await client.post(base + "/claim", headers=headers, json=req)).json()["result"][
            "owner"
        ] is False
        evt = {
            "version": 1,
            "seq": 1,
            "event": {
                "type": "tool_execution_end",
                "tool_call_id": "t1",
                "tool_name": "execute",
                "result": {"content": [{"type": "text", "text": "test"}]},
            },
        }
        responses = await asyncio.gather(
            *(client.post(base + "/events", headers=headers, json=evt) for _ in range(5))
        )
        assert all(response.status_code == 200 for response in responses), [
            r.text for r in responses
        ]
        assert sum(not response.json()["result"]["duplicate"] for response in responses) == 1
        count = await redis.xlen(_run_events_key(prefix, d.run_id))
        assert count > 0
        conflict = {**evt, "event": {**evt["event"], "tool_call_id": "t2"}}
        assert (
            await client.post(base + "/events", headers=headers, json=conflict)
        ).status_code == 409
        assert (
            await client.post(base + "/events", headers=headers, json={**evt, "seq": 3})
        ).status_code == 409
        assert await redis.xlen(_run_events_key(prefix, d.run_id)) == count
        async with async_session_maker() as session:
            row = await session.get(AgentCoreDispatch, d.id)
            assert row is not None
            row.heartbeat_at = datetime.now(UTC) - timedelta(minutes=5)
            await session.commit()
        assert (await client.get(base + "/control", headers=headers)).status_code == 200
        async with async_session_maker() as session:
            row = await session.get(AgentCoreDispatch, d.id)
            assert (
                row
                and row.heartbeat_at
                and (datetime.now(UTC) - row.heartbeat_at).total_seconds() < 5
            )
        assert await redis.get(_active_run_key(prefix, d.conversation_id)) == d.run_id
        assert await redis.hget(_run_meta_key(prefix, d.run_id), "last_event_at")


async def test_model_secret_is_not_returned_and_uncertain_call_is_not_replayed(
    cp_task: Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, d = cp_task
    async with async_session_maker() as session:
        row = await session.get(AgentCoreDispatch, d.id)
        assert row is not None
        row.request = {
            **row.request,
            "model_ref": "test/fixture",
            "model": {"id": "fixture", "max_output_tokens": 2048},
        }
        await session.commit()
    secret = "test-model-secret-not-returned"

    async def snapshot(*args: Any) -> Any:
        return SimpleNamespace(
            providers={
                "test": SimpleNamespace(
                    api="openai-responses",
                    api_key=secret,
                    base_url="https://model.invalid/v1",
                    models=[SimpleNamespace(id="fixture")],
                )
            }
        )

    monkeypatch.setattr(native_model, "load_llm_snapshot", snapshot)
    calls = 0

    async def upstream(body: Any, base_url: str, api_key: str) -> str:
        nonlocal calls
        calls += 1
        assert api_key == secret
        return 'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'

    monkeypatch.setattr(native_model, "_upstream", upstream)
    body = {
        "model": "fixture",
        "input": "test",
        "max_output_tokens": 2048,
        "stream": True,
        "store": False,
    }
    req = uuid4()
    response = await native_model.proxy_model(d.id, req, body, encryption_backend=object())
    assert await native_model.proxy_model(d.id, req, body, encryption_backend=object()) == response
    assert calls == 1 and secret not in response
    lost = uuid4()
    async with callback_transaction(d.id, lost, "model", body):
        pass
    with pytest.raises(NativeTaskError) as unknown:
        await native_model.proxy_model(d.id, lost, body, encryption_backend=object())
    assert unknown.value.code == "native_callback_outcome_unknown"
    assert calls == 1


async def test_wire_tool_result_restores_presented_file_event(cp_task: Task) -> None:
    from cubeloop.agent.types import AgentToolResult, ToolExecutionEndEvent
    from cubeloop.providers.base import TextContent

    from cubeplex.agentcore.native_events import receive_event

    redis, prefix, d = cp_task
    result = AgentToolResult(
        content=[
            TextContent(
                text=json.dumps(
                    {
                        "action": "presented",
                        "presented_file": {
                            "id": "pfile-test",
                            "filename": "result.txt",
                            "kind": "document",
                            "size_bytes": 3,
                        },
                    }
                )
            )
        ]
    )
    wire = ToolExecutionEndEvent(
        tool_call_id="tool-1", tool_name="present_file", result=result
    ).model_dump(mode="json")
    assert isinstance(wire["result"], dict)
    response = await receive_event(
        redis, prefix=prefix, dispatch_id=d.id, seq=1, event=wire, ttl_seconds=120, maxlen=1000
    )
    rows = await redis.xrange(_run_events_key(prefix, d.run_id))
    payloads = [json.loads(fields["payload"]) for _, fields in rows]
    assert [payload["type"] for payload in payloads] == ["tool_result", "presented_file"]
    assert payloads[1]["data"]["presented_file"]["id"] == "pfile-test"
    assert len(response["event_ids"]) == 2


async def test_pending_clear_is_sql_null_for_official_checkpointer(cp_task: Task) -> None:
    from sqlalchemy import text

    _, _, d = cp_task
    request = {
        "question_id": "q-clear",
        "thread_id": d.conversation_id,
        "created_at": datetime.now(UTC).timestamp(),
        "payload": {"kind": "ask", "questions": [{"key": "pick", "prompt": "Choose"}]},
    }
    await checkpoint_call(d.id, uuid4(), "save_pending_request", {"request": request})
    async with shared_checkpointer() as cp:
        assert await cp.load_pending_request(d.conversation_id) is not None
    request_id = uuid4()
    await checkpoint_call(d.id, request_id, "save_pending_request", {"request": None})
    assert (
        await checkpoint_call(d.id, request_id, "save_pending_request", {"request": None}) is None
    )
    async with shared_checkpointer() as cp:
        assert await cp.load_pending_request(d.conversation_id) is None
        assert await cp.load_pending(d.conversation_id) is None
        assert await cp.load_pending_run_id(d.conversation_id) is None
    async with async_session_maker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT pending_request IS NULL, run_id IS NULL FROM cubepi_threads WHERE thread_id=:thread"
                ),
                {"thread": d.conversation_id},
            )
        ).one()
        assert row == (True, True)


async def test_terminal_receipt_hash_preserves_semantics_and_ignores_timestamp(
    cp_task: Task,
) -> None:
    from cubeplex.agentcore.native_events import emit_native_terminal

    redis, prefix, d = cp_task
    initial = {
        "type": "error",
        "timestamp": "2026-09-13T00:00:00+00:00",
        "data": {"error_code": "first_failure", "message": "first"},
    }
    kwargs = {
        "prefix": prefix,
        "dispatch_id": str(d.id),
        "run_id": d.run_id,
        "conversation_id": d.conversation_id,
        "ttl_seconds": 120,
        "maxlen": 1000,
    }
    first = await emit_native_terminal(redis, status="errored", events=[initial], **kwargs)
    duplicate = await emit_native_terminal(
        redis,
        status="errored",
        events=[{**initial, "timestamp": "2026-09-13T00:01:00+00:00"}],
        **kwargs,
    )
    assert duplicate["duplicate"] and duplicate["event_ids"] == first["event_ids"]
    for status, event in (
        ("completed", initial),
        (
            "errored",
            {**initial, "data": {"error_code": "different_failure", "message": "different"}},
        ),
    ):
        with pytest.raises(ValueError, match="native_event_conflict"):
            await emit_native_terminal(redis, status=status, events=[event], **kwargs)
    assert await redis.xlen(_run_events_key(prefix, d.run_id)) == 1
    assert await redis.hget(_run_meta_key(prefix, d.run_id), "status") == "errored"
