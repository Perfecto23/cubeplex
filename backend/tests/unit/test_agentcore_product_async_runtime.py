"""Short AgentCore invocation and SDK health tracking contracts."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from cubeplex.agentcore import runtime
from cubeplex.agentcore.dispatch import agentcore_session_id
from cubeplex.agentcore.worker import AgentCoreWorker, WorkerInvocationResult
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch


def _dispatch(*, status: str = "claimed") -> AgentCoreDispatch:
    dispatch = AgentCoreDispatch(
        org_id="o1",
        workspace_id="w1",
        conversation_id="c1",
        user_id="u1",
        run_id="r1",
        operation="prompt",
        request={},
        status=status,
        session_id="",
    )
    dispatch.session_id = agentcore_session_id(dispatch.id)
    return dispatch


def _worker() -> AgentCoreWorker:
    return AgentCoreWorker(
        session_maker=MagicMock(),
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_real_sdk_http_accept_survives_request_and_tracks_busy_until_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch()
    worker = _worker()
    first_prepare = (dispatch, None)
    duplicate = WorkerInvocationResult(
        dispatch_id=dispatch.id,
        status="claimed",
        duplicate=True,
    )
    monkeypatch.setattr(
        worker,
        "_prepare_dispatch",
        AsyncMock(side_effect=[first_prepare, (None, duplicate)]),
    )
    started = threading.Event()
    finished = threading.Event()
    executions = 0

    async def execute(_: AgentCoreDispatch) -> WorkerInvocationResult:
        nonlocal executions
        executions += 1
        started.set()
        await asyncio.sleep(0.15)
        finished.set()
        return WorkerInvocationResult(dispatch_id=dispatch.id, status="finished")

    monkeypatch.setattr(worker, "_execute_claimed", execute)
    monkeypatch.setattr(runtime, "_worker", worker)
    payload = {"version": 1, "dispatch_id": str(dispatch.id)}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app),
        base_url="http://agentcore.test",
    ) as client:
        response = await client.post(
            "/invocations",
            headers={
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": dispatch.session_id,
            },
            json=payload,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        assert started.wait(timeout=1)
        assert (await client.get("/ping")).json()["status"] == "HealthyBusy"

        duplicate_response = await client.post(
            "/invocations",
            headers={
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": dispatch.session_id,
            },
            json=payload,
        )
        assert duplicate_response.json()["duplicate"] is True
        assert duplicate_response.json()["status"] == "claimed"

        # Closing the request context has already happened for both calls;
        # completion is owned by the SDK worker-loop task instead.
        assert await asyncio.to_thread(finished.wait, 1)
        assert (await client.get("/ping")).json()["status"] == "Healthy"

    assert executions == 1
    assert not worker._background_tasks


@pytest.mark.asyncio
async def test_tracked_task_completion_clears_sdk_health_after_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch()
    worker = _worker()
    monkeypatch.setattr(worker, "_prepare_dispatch", AsyncMock(return_value=(dispatch, None)))

    async def fail(_: AgentCoreDispatch) -> WorkerInvocationResult:
        raise RuntimeError("execution failed")

    monkeypatch.setattr(worker, "_execute_claimed", fail)
    monkeypatch.setattr(runtime, "_worker", worker)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.app),
        base_url="http://agentcore.test",
    ) as client:
        response = await client.post(
            "/invocations",
            headers={
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": dispatch.session_id,
            },
            json={"version": 1, "dispatch_id": str(dispatch.id)},
        )
        assert response.json()["status"] == "accepted"
        for _ in range(50):
            if not worker._background_tasks:
                break
            await asyncio.sleep(0.01)
        assert not worker._background_tasks
        assert (await client.get("/ping")).json()["status"] == "Healthy"


@pytest.mark.asyncio
async def test_accept_without_sdk_tracking_fails_before_native_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch(status="created")
    worker = _worker()
    monkeypatch.setattr(worker, "_prepare_dispatch", AsyncMock(return_value=(dispatch, None)))
    fail_native = AsyncMock()
    mark_finished = AsyncMock()
    monkeypatch.setattr(worker, "_fail_native_run", fail_native)
    monkeypatch.setattr("cubeplex.agentcore.worker.mark_dispatch_finished", mark_finished)

    result = await worker.accept(
        {"version": 1, "dispatch_id": str(dispatch.id)},
        session_id=dispatch.session_id,
        app=SimpleNamespace(),
    )

    assert result.status == "finished"
    assert result.error_code == "worker_tracking_unavailable"
    fail_native.assert_awaited_once_with(dispatch, "worker_tracking_unavailable")
    mark_finished.assert_awaited_once()
