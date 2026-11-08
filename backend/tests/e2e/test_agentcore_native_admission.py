"""Native callbacks honor the same durable admission as the Web run entrance."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete

from cubeplex.agentcore.native_auth import issue_capability
from cubeplex.agentcore.native_lifecycle import finish_native_dispatch, reconcile_native_finish
from cubeplex.agentcore.native_router import router
from cubeplex.config import config
from cubeplex.db.engine import async_session_maker
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch
from cubeplex.models.conversation_execution import ConversationExecutionAdmission
from cubeplex.streams.run_events import _active_run_key, _run_events_key, _run_meta_key
from tests.e2e.test_agentcore_native_lifecycle import Task, native_task  # noqa: F401

pytestmark = pytest.mark.asyncio
AdmittedTask = tuple[Task, str, httpx.AsyncClient]


@pytest_asyncio.fixture
async def admitted_native(native_task: Task) -> AsyncIterator[AdmittedTask]:  # noqa: F811
    redis, prefix, dispatch = native_task
    token = str(uuid4())
    admission = ConversationExecutionAdmission(
        org_id=dispatch.org_id,
        workspace_id=dispatch.workspace_id,
        conversation_id=dispatch.conversation_id,
        actor_user_id=dispatch.user_id,
        source_kind="user_message",
        source_id=f"web:{uuid4()}",
        execution_generation=0,
        run_id=dispatch.run_id,
        run_start_token=token,
        run_start_requested_at=datetime.now(UTC),
        resolved_execution={"model_key": "test", "primary": "test/model"},
    )
    async with async_session_maker() as session:
        session.add(admission)
        row = await session.get(AgentCoreDispatch, dispatch.id)
        assert row is not None
        row.status = "created"
        row.request = {
            **row.request,
            "context": {
                **row.request["context"],
                "execution": {
                    "admission_id": admission.id,
                    "generation": 0,
                    "start_token": token,
                    "attempt_id": token,
                },
            },
            "system_prompt": "Native admission test",
            "model": {"id": "model", "max_output_tokens": 64},
            "content": "complete",
        }
        await session.commit()
    await redis.hset(_run_meta_key(prefix, dispatch.run_id), "claim_token", token)
    original_secret = config.get("auth.jwt_secret")
    config.set("auth.jwt_secret", secrets.token_hex(32))
    app = FastAPI()
    app.state.redis = redis
    app.state.redis_key_prefix = prefix
    app.include_router(router, prefix="/api/v1")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=f"https://native.invalid/api/v1/agentcore/tasks/{dispatch.id}/",
            headers={"Authorization": "Bearer " + issue_capability(dispatch.id)},
        ) as client:
            yield native_task, admission.id, client
    finally:
        config.set("auth.jwt_secret", original_secret)
        async with async_session_maker() as session:
            await session.execute(
                delete(ConversationExecutionAdmission).where(
                    ConversationExecutionAdmission.id == admission.id
                )
            )
            await session.commit()


def envelope(**fields: Any) -> dict[str, Any]:
    return {"version": 1, "request_id": str(uuid4()), **fields}


async def claim(client: httpx.AsyncClient) -> dict[str, Any]:
    body = envelope()
    response = await client.post("claim", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["owner"] is True
    replay = await client.post("claim", json=body)
    assert replay.status_code == 200 and replay.json()["result"]["owner"] is False
    return body


async def completed_checkpoint(client: httpx.AsyncClient) -> dict[str, Any]:
    operations = [
        envelope(op="claim_run", args={}),
        envelope(
            op="append",
            args={
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "complete"}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                ]
            },
        ),
        envelope(op="mark_run_complete", args={}),
    ]
    for operation in operations:
        response = await client.post("checkpoint", json=operation)
        assert response.status_code == 200, response.text
    response = await client.post("workspace", json=envelope(files=[]))
    assert response.status_code == 200, response.text
    return operations[1]


async def test_native_claim_and_finish_record_the_original_admission(
    admitted_native: AdmittedTask,
) -> None:
    task, admission_id, client = admitted_native
    redis, prefix, dispatch = task
    await claim(client)
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None and admission.run_started_at is not None
        assert admission.run_finished_at is None
    append = await completed_checkpoint(client)
    request = envelope(status="completed", error_code=None)
    response = await client.post("finish", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"] == "completed"
    assert (await client.post("finish", json=request)).json() == response.json()
    assert (await client.post("checkpoint", json=append)).status_code == 200
    late = await client.post("checkpoint", json=envelope(op="save_extra", args={"extra": {}}))
    assert late.status_code == 409
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        assert admission.run_terminal_status == "completed"
        assert admission.run_terminal_at is not None and admission.run_finished_at is not None
    assert await redis.get(_active_run_key(prefix, dispatch.conversation_id)) is None
    assert await redis.xlen(_run_events_key(prefix, dispatch.run_id)) == 1


async def test_durable_stop_blocks_work_and_control_reports_cancellation(
    admitted_native: AdmittedTask,
) -> None:
    _, admission_id, client = admitted_native
    await claim(client)
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        admission.run_stop_requested_at = datetime.now(UTC)
        await session.commit()
    blocked = await client.post("checkpoint", json=envelope(op="claim_run", args={}))
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "native_execution_revoked"
    control = await client.get("control")
    assert control.status_code == 200 and control.json()["result"]["stop_requested"] is True
    result = await client.post("finish", json=envelope(status="cancelled", error_code=None))
    assert result.status_code == 200, result.text
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        assert (
            admission.run_terminal_status == "cancelled" and admission.run_finished_at is not None
        )


async def test_replaced_attempt_cannot_write_or_renew_heartbeat(
    admitted_native: AdmittedTask,
) -> None:
    (redis, prefix, dispatch), _, client = admitted_native
    await claim(client)
    key = _run_meta_key(prefix, dispatch.run_id)
    old_heartbeat = await redis.hget(key, "last_event_at")
    replacement = str(uuid4())
    await redis.hset(key, "claim_token", replacement)
    checkpoint = await client.post("checkpoint", json=envelope(op="claim_run", args={}))
    assert checkpoint.status_code == 409
    assert checkpoint.json()["detail"]["code"] == "native_claim_conflict"
    control = await client.get("control")
    assert control.status_code == 409
    assert await redis.hget(key, "last_event_at") == old_heartbeat
    assert await redis.hget(key, "claim_token") == replacement
    assert (await client.post("workspace", json=envelope(files=[]))).status_code == 409


async def test_stop_before_native_claim_records_no_worker_entry(
    admitted_native: AdmittedTask,
) -> None:
    _, admission_id, client = admitted_native
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        admission.run_stop_requested_at = datetime.now(UTC)
        await session.commit()
    response = await client.post("claim", json=envelope())
    assert response.status_code == 409
    result = await client.post("finish", json=envelope(status="cancelled", error_code=None))
    assert result.status_code == 200, result.text
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        assert admission.run_started_at is None
        assert (
            admission.run_terminal_status == "cancelled" and admission.run_finished_at is not None
        )


async def test_native_finish_recovers_after_redis_release_failure_without_reexecuting(
    admitted_native: AdmittedTask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cubeplex.streams import run_events

    (redis, prefix, dispatch), admission_id, client = admitted_native
    await claim(client)
    await completed_checkpoint(client)
    original_release = run_events.clear_active_run

    async def failed_release(*args: Any, **kwargs: Any) -> bool:
        raise OSError("release acknowledgement lost")

    monkeypatch.setattr(run_events, "clear_active_run", failed_release)
    with pytest.raises(OSError, match="release acknowledgement"):
        await finish_native_dispatch(
            redis,
            prefix=prefix,
            dispatch_id=dispatch.id,
            request_id=uuid4(),
            status="completed",
            error_code=None,
            ttl_seconds=120,
            maxlen=1000,
        )
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        assert admission.run_terminal_status == "completed" and admission.run_finished_at is None
        row = await session.get(AgentCoreDispatch, dispatch.id)
        assert row is not None and row.status == "claimed"
    assert await redis.get(_active_run_key(prefix, dispatch.conversation_id)) == dispatch.run_id
    monkeypatch.setattr(run_events, "clear_active_run", original_release)
    assert await reconcile_native_finish(
        redis, prefix=prefix, dispatch_id=dispatch.id, ttl_seconds=120, maxlen=1000
    )
    assert not await reconcile_native_finish(
        redis, prefix=prefix, dispatch_id=dispatch.id, ttl_seconds=120, maxlen=1000
    )
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None and admission.run_finished_at is not None
        row = await session.get(AgentCoreDispatch, dispatch.id)
        assert row is not None and row.status == "finished"
    assert await redis.get(_active_run_key(prefix, dispatch.conversation_id)) is None
    assert await redis.xlen(_run_events_key(prefix, dispatch.run_id)) == 1


async def test_admitted_native_hitl_keeps_start_token_across_fresh_worker_attempt(
    admitted_native: AdmittedTask, tmp_path: Path
) -> None:
    from cubeloop.providers.faux import FauxProvider, faux_assistant_message, faux_tool_call

    from cubeplex.agentcore.dispatch import agentcore_session_id
    from cubeplex.agents.checkpointer import shared_checkpointer
    from cubeplex.streams.hitl_resume import claim_resume
    from tests.e2e.test_agentcore_native_http_worker import (
        NativeControlPlaneClient,
        NativeWorker,
        Workspace,
    )

    (redis, prefix, dispatch), admission_id, client = admitted_native
    first_provider = FauxProvider(provider_id="native-admission-first")
    first_provider.set_responses(
        [
            faux_assistant_message(
                faux_tool_call(
                    "ask_user", {"questions": [{"key": "choice", "prompt": "Continue?"}]}
                )
            )
        ]
    )
    worker_client = NativeControlPlaneClient(
        base_url="https://native.invalid",
        dispatch_id=str(dispatch.id),
        capability=issue_capability(dispatch.id),
        http_client=client,
    )
    result = await asyncio.wait_for(
        NativeWorker(
            worker_client,
            workspace=Workspace(tmp_path / "first"),
            model_factory=lambda _client, _config: first_provider,
        ).run(),
        20,
    )
    assert result["status"] == "paused_hitl", result
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None and admission.run_started_at is not None
        assert admission.run_finished_at is None and admission.run_terminal_status is None
        start_token, started_at = admission.run_start_token, admission.run_started_at
        original = await session.get(AgentCoreDispatch, dispatch.id)
        assert original is not None and original.status == "finished"
        original_request = original.request
    async with shared_checkpointer() as cp:
        pending = await cp.load_pending(dispatch.conversation_id)
    assert pending is not None and pending[1] == dispatch.run_id
    assert await redis.get(_active_run_key(prefix, dispatch.conversation_id)) == dispatch.run_id
    resumed = await claim_resume(
        redis,
        prefix=prefix,
        conversation_id=dispatch.conversation_id,
        expected_run_id=dispatch.run_id,
        started_at=started_at.isoformat(),
        ttl_seconds=120,
    )
    assert resumed.claim_token is not None and resumed.claim_token != start_token
    respond = AgentCoreDispatch(
        org_id=dispatch.org_id,
        workspace_id=dispatch.workspace_id,
        conversation_id=dispatch.conversation_id,
        user_id=dispatch.user_id,
        run_id=dispatch.run_id,
        operation="respond",
        claim_token=resumed.claim_token,
        session_id="",
        request={
            **original_request,
            "question_id": pending[0].question_id,
            "answer": {"choice": "yes"},
            "claim_token": resumed.claim_token,
            "context": {
                **original_request["context"],
                "execution": {
                    **original_request["context"]["execution"],
                    "attempt_id": resumed.claim_token,
                },
            },
        },
    )
    respond.session_id = agentcore_session_id(respond.id)
    async with async_session_maker() as session:
        session.add(respond)
        await session.commit()
    next_provider = FauxProvider(provider_id="native-admission-next")
    next_provider.set_responses([faux_assistant_message("continued")])
    next_client = NativeControlPlaneClient(
        base_url="https://native.invalid",
        dispatch_id=str(respond.id),
        capability=issue_capability(respond.id),
        http_client=client,
    )
    result = await asyncio.wait_for(
        NativeWorker(
            next_client,
            workspace=Workspace(tmp_path / "second"),
            model_factory=lambda _client, _config: next_provider,
        ).run(),
        20,
    )
    assert result["status"] == "completed", result
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None and admission.run_terminal_status == "completed"
        assert admission.run_finished_at is not None
        assert admission.run_started_at == started_at and admission.run_start_token == start_token
    assert await redis.get(_active_run_key(prefix, dispatch.conversation_id)) is None


async def test_expired_native_finish_only_records_cleanup_and_preserves_a_new_run(
    admitted_native: AdmittedTask, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cubeplex.agentcore import native_events

    (redis, prefix, dispatch), admission_id, client = admitted_native
    await claim(client)
    await completed_checkpoint(client)
    original_emit = native_events.emit_native_terminal

    async def unavailable_redis(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise OSError("terminal projection unavailable")

    monkeypatch.setattr(native_events, "emit_native_terminal", unavailable_redis)
    with pytest.raises(OSError, match="projection unavailable"):
        await finish_native_dispatch(
            redis,
            prefix=prefix,
            dispatch_id=dispatch.id,
            request_id=uuid4(),
            status="completed",
            error_code=None,
            ttl_seconds=120,
            maxlen=1000,
        )
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        assert admission.run_terminal_status == "completed" and admission.run_finished_at is None
    await redis.delete(
        _run_meta_key(prefix, dispatch.run_id),
        _run_events_key(prefix, dispatch.run_id),
        _active_run_key(prefix, dispatch.conversation_id),
    )
    next_run = str(uuid4())
    await redis.set(_active_run_key(prefix, dispatch.conversation_id), next_run, ex=120)
    monkeypatch.setattr(native_events, "emit_native_terminal", original_emit)
    assert await reconcile_native_finish(
        redis, prefix=prefix, dispatch_id=dispatch.id, ttl_seconds=120, maxlen=1000
    )
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        assert (
            admission.run_terminal_status == "completed" and admission.run_finished_at is not None
        )
        row = await session.get(AgentCoreDispatch, dispatch.id)
        assert row is not None and row.status == "finished"
    assert await redis.get(_active_run_key(prefix, dispatch.conversation_id)) == next_run
    assert not await redis.exists(_run_meta_key(prefix, dispatch.run_id))
    assert not await redis.exists(_run_events_key(prefix, dispatch.run_id))


async def test_checkpoint_conditional_clear_preserves_a_replacement_question(
    admitted_native: AdmittedTask,
) -> None:
    from cubeplex_native.checkpointer import RemoteCheckpointer

    from tests.e2e.test_agentcore_native_http_worker import NativeControlPlaneClient

    (_, _, dispatch), _, client = admitted_native
    await claim(client)
    native = NativeControlPlaneClient(
        base_url="https://native.invalid",
        dispatch_id=str(dispatch.id),
        capability=issue_capability(dispatch.id),
        http_client=client,
    )
    cp = RemoteCheckpointer(native, dispatch.conversation_id)
    await cp.claim_run(dispatch.conversation_id, dispatch.run_id)
    from cubeloop.hitl.types import HitlRequest

    pending = HitlRequest.model_validate(
        {
            "question_id": "replacement",
            "thread_id": dispatch.conversation_id,
            "created_at": datetime.now(UTC).timestamp(),
            "payload": {"kind": "ask", "questions": [{"key": "pick", "prompt": "Choose"}]},
        }
    )
    await cp.save_pending_request(dispatch.conversation_id, pending, run_id=dispatch.run_id)
    assert not await cp.clear_pending_request_if_matches(
        dispatch.conversation_id, question_id="obsolete", run_id=dispatch.run_id
    )
    assert not await cp.clear_pending_request_if_matches(
        dispatch.conversation_id, question_id="replacement", run_id="other-run"
    )
    still_pending = await cp.load_pending_request(dispatch.conversation_id)
    assert still_pending is not None and still_pending.question_id == "replacement"
    assert await cp.clear_pending_request_if_matches(
        dispatch.conversation_id, question_id="replacement", run_id=dispatch.run_id
    )
    assert await cp.load_pending_request(dispatch.conversation_id) is None


@pytest.mark.parametrize("worker_started", [False, True])
async def test_confirmed_native_teardown_cancels_expired_dispatch_without_prepared_finish(
    admitted_native: AdmittedTask, worker_started: bool
) -> None:
    from cubeloop.hitl.types import HitlRequest

    from cubeplex.agentcore.dispatch import request_dispatch_stop
    from cubeplex.agentcore.native_lifecycle import cancel_native_dispatch
    from cubeplex.agents.checkpointer import shared_checkpointer

    (redis, prefix, dispatch), admission_id, client = admitted_native
    if worker_started:
        await claim(client)
        claimed = await client.post("checkpoint", json=envelope(op="claim_run", args={}))
        assert claimed.status_code == 200, claimed.text
    await request_dispatch_stop(async_session_maker, run_id=dispatch.run_id)
    await redis.delete(
        _run_meta_key(prefix, dispatch.run_id),
        _run_events_key(prefix, dispatch.run_id),
        _active_run_key(prefix, dispatch.conversation_id),
    )
    replacement_run = str(uuid4())
    await redis.set(_active_run_key(prefix, dispatch.conversation_id), replacement_run, ex=120)
    new_question = HitlRequest.model_validate(
        {
            "question_id": "new-run-question",
            "thread_id": dispatch.conversation_id,
            "created_at": datetime.now(UTC).timestamp(),
            "payload": {"kind": "ask", "questions": [{"key": "pick", "prompt": "Choose"}]},
        }
    )
    async with shared_checkpointer() as cp:
        await cp.claim_run(dispatch.conversation_id, replacement_run)
        await cp.save_pending_request(
            dispatch.conversation_id, new_question, run_id=replacement_run
        )
    untrusted = await client.post("finish", json=envelope(status="cancelled", error_code=None))
    assert untrusted.status_code == 409
    assert untrusted.json()["detail"]["code"] == "native_claim_conflict"
    forged_confirmation = await client.post(
        "finish", json=envelope(status="cancelled", error_code=None, teardown_confirmed=True)
    )
    assert forged_confirmation.status_code == 422

    class RuntimeStop:
        confirmed = False
        calls: list[UUID] = []

        async def stop(self, dispatch_id: UUID) -> dict[str, int]:
            self.calls.append(dispatch_id)
            if not self.confirmed:
                raise TimeoutError("teardown unknown")
            return {"statusCode": 200}

    runtime = RuntimeStop()
    assert (
        await cancel_native_dispatch(
            dispatch,
            client=runtime,
            redis=redis,
            prefix=prefix,
            ttl_seconds=120,
            maxlen=1000,
        )
        == "stop_unknown"
    )
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        assert admission.run_finished_at is None and admission.run_terminal_status is None
    runtime.confirmed = True
    assert (
        await cancel_native_dispatch(
            dispatch,
            client=runtime,
            redis=redis,
            prefix=prefix,
            ttl_seconds=120,
            maxlen=1000,
        )
        == "cancelled"
    )
    assert runtime.calls == [dispatch.id, dispatch.id]
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        assert (admission.run_started_at is not None) is worker_started
        assert (
            admission.run_terminal_status == "cancelled" and admission.run_finished_at is not None
        )
        row = await session.get(AgentCoreDispatch, dispatch.id)
        assert row is not None and row.status == "finished"
    assert await redis.get(_active_run_key(prefix, dispatch.conversation_id)) == replacement_run
    assert not await redis.exists(_run_meta_key(prefix, dispatch.run_id))
    assert not await redis.exists(_run_events_key(prefix, dispatch.run_id))
    async with shared_checkpointer() as cp:
        pending = await cp.load_pending(dispatch.conversation_id)
    assert pending is not None and pending[1] == replacement_run
    assert pending[0].question_id == new_question.question_id
