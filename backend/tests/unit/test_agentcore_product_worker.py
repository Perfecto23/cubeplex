"""Worker-side duplicate, scope and session contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cubeplex.agentcore.dispatch import (
    DispatchValidationError,
    agentcore_session_id,
    invocation_payload,
)
from cubeplex.agentcore.worker import AgentCoreWorker
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch


def _dispatch(*, status: str = "created") -> AgentCoreDispatch:
    dispatch = AgentCoreDispatch(
        org_id="o1",
        workspace_id="w1",
        conversation_id="c1",
        user_id="u1",
        run_id="r1",
        operation="prompt",
        request={
            "content": "hello",
            "attachments": [],
            "context": {
                "user_id": "u1",
                "org_id": "o1",
                "workspace_id": "w1",
                "conversation_id": "c1",
                "trigger": "interactive",
            },
            "reasoning": {},
        },
        status=status,
        session_id="",
    )
    dispatch.session_id = agentcore_session_id(dispatch.id)
    return dispatch


@pytest.mark.asyncio
async def test_duplicate_claim_is_acknowledged_without_manager_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch(status="claimed")
    monkeypatch.setattr(
        "cubeplex.agentcore.worker.claim_dispatch",
        AsyncMock(return_value=(dispatch, False)),
    )
    worker = AgentCoreWorker(
        session_maker=MagicMock(),
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(side_effect=AssertionError("must not execute")),
    )
    result = await worker.invoke(
        invocation_payload(dispatch.id),
        session_id=dispatch.session_id,
    )
    assert result.duplicate is True
    assert result.status == "claimed"


@pytest.mark.asyncio
async def test_session_mismatch_is_rejected_before_claim() -> None:
    worker = AgentCoreWorker(
        session_maker=AsyncMock(),
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(),
    )
    with pytest.raises(Exception, match="session_scope_mismatch"):
        await worker.invoke(invocation_payload(uuid4()), session_id="wrong")


@pytest.mark.asyncio
async def test_claimed_worker_validates_native_scope_before_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch()
    monkeypatch.setattr(
        "cubeplex.agentcore.worker.claim_dispatch",
        AsyncMock(return_value=(dispatch, True)),
    )
    worker = AgentCoreWorker(
        session_maker=MagicMock(),
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(side_effect=AssertionError("scope must fail first")),
    )
    monkeypatch.setattr(
        worker,
        "_validate_native_scope",
        AsyncMock(side_effect=DispatchValidationError("scope mismatch")),
    )
    with pytest.raises(Exception, match="scope mismatch"):
        await worker.invoke(
            invocation_payload(dispatch.id),
            session_id=dispatch.session_id,
        )


@pytest.mark.asyncio
async def test_respond_old_claim_is_rejected_before_pending_check() -> None:
    dispatch = _dispatch()
    dispatch.operation = "respond"
    dispatch.request = {
        "question_id": "q-old",
        "claim_token": "claim-old",
        "context": dispatch.request["context"],
    }
    redis = MagicMock()
    redis.hget = AsyncMock(return_value="claim-current")
    worker = AgentCoreWorker(
        session_maker=MagicMock(),
        redis=redis,
        redis_key_prefix="p",
        manager_factory=AsyncMock(),
    )

    with pytest.raises(DispatchValidationError, match="claim_mismatch"):
        await worker._validate_respond_claim(dispatch)


@pytest.mark.asyncio
async def test_worker_start_failure_has_error_done_and_active_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch()
    update = AsyncMock()
    append = AsyncMock()
    clear = AsyncMock()
    expire = AsyncMock()
    monkeypatch.setattr("cubeplex.agentcore.worker.update_run_meta", update)
    monkeypatch.setattr("cubeplex.agentcore.worker.append_run_event", append)
    monkeypatch.setattr("cubeplex.agentcore.worker.clear_active_run", clear)
    monkeypatch.setattr("cubeplex.agentcore.worker.expire_run_data", expire)
    worker = AgentCoreWorker(
        session_maker=MagicMock(),
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(),
    )

    await worker._fail_native_run(dispatch, "worker_start_failed")

    assert update.await_args.kwargs["status"] == "errored"
    assert append.await_count == 2
    assert append.await_args_list[0].kwargs["payload"]["type"] == "error"
    assert append.await_args_list[1].kwargs["payload"]["type"] == "done"
    clear.assert_awaited_once()
    expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_parser_initialization_failure_cleans_native_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch()
    monkeypatch.setattr(
        "cubeplex.agentcore.worker.claim_dispatch",
        AsyncMock(return_value=(dispatch, True)),
    )
    monkeypatch.setattr(
        "cubeplex.agentcore.worker.mark_dispatch_finished",
        AsyncMock(),
    )
    worker = AgentCoreWorker(
        session_maker=MagicMock(),
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(),
    )
    monkeypatch.setattr(worker, "_validate_native_scope", AsyncMock())
    monkeypatch.setattr(
        worker,
        "_ensure_runtime_dependencies",
        AsyncMock(side_effect=RuntimeError("parser discovery failed")),
    )
    fail_native = AsyncMock()
    monkeypatch.setattr(worker, "_fail_native_run", fail_native)

    result = await worker.invoke(
        invocation_payload(dispatch.id),
        session_id=dispatch.session_id,
    )
    assert result.error_code == "worker_execution_failed"
    fail_native.assert_awaited_once_with(dispatch, "worker_start_failed")


@pytest.mark.asyncio
async def test_stop_requested_before_execution_is_cancelled_without_worker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch()
    dispatch.stop_requested = True
    monkeypatch.setattr(
        "cubeplex.agentcore.worker.claim_dispatch",
        AsyncMock(return_value=(dispatch, True)),
    )
    monkeypatch.setattr(
        "cubeplex.agentcore.worker.mark_dispatch_finished",
        AsyncMock(),
    )
    worker = AgentCoreWorker(
        session_maker=MagicMock(),
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(side_effect=AssertionError("must not execute")),
    )
    monkeypatch.setattr(worker, "_validate_native_scope", AsyncMock())
    monkeypatch.setattr(worker, "_ensure_runtime_dependencies", AsyncMock())
    cancel_native = AsyncMock()
    fail_native = AsyncMock()
    monkeypatch.setattr(worker, "_cancel_native_run", cancel_native)
    monkeypatch.setattr(worker, "_fail_native_run", fail_native)

    result = await worker.invoke(
        invocation_payload(dispatch.id),
        session_id=dispatch.session_id,
    )

    assert result.status == "finished"
    assert result.error_code == "cancelled"
    cancel_native.assert_awaited_once_with(dispatch)
    fail_native.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,should_ack", [("finished", True), ("stop_unknown", False)])
async def test_worker_cancel_ack_is_gated_by_durable_dispatch(
    status: str,
    should_ack: bool,
) -> None:
    dispatch = _dispatch(status="claimed")
    worker = AgentCoreWorker(
        session_maker=MagicMock(),
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(),
    )
    session = MagicMock()
    session.get = AsyncMock(
        return_value=SimpleNamespace(
            status=status,
            stop_requested=True,
        )
    )
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    worker._session_maker = MagicMock(return_value=session)
    publish_ack = AsyncMock()

    await worker._publish_confirmed_cancel_ack(
        SimpleNamespace(_publish_ack=publish_ack),
        dispatch,
    )

    if should_ack:
        publish_ack.assert_awaited_once_with(dispatch.run_id)
    else:
        publish_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_sandbox_provision_persists_before_listener_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = _dispatch(status="claimed")
    row = SimpleNamespace(stop_requested=False, status="claimed")
    session = MagicMock()
    session.get = AsyncMock(side_effect=lambda *_args: row)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session_maker = MagicMock(return_value=session)
    native_started = asyncio.Event()
    native_cancelled = asyncio.Event()
    terminal_persisted = asyncio.Event()
    listeners_stopped = asyncio.Event()

    async def mark_finished_side_effect(*_args: object, **_kwargs: object) -> None:
        row.status = "finished"
        terminal_persisted.set()

    mark_finished = AsyncMock(side_effect=mark_finished_side_effect)
    monkeypatch.setattr(
        "cubeplex.agentcore.worker.mark_dispatch_finished",
        mark_finished,
    )

    class ProvisioningManager:
        native_task: asyncio.Task[None] | None = None

        async def start_control_listeners(self) -> None:
            return

        async def stop_control_listeners(self) -> None:
            assert terminal_persisted.is_set()
            listeners_stopped.set()

        async def execute_agentcore_dispatch(self, _dispatch: AgentCoreDispatch) -> None:
            type(self).native_task = asyncio.current_task()
            native_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                native_cancelled.set()
                raise

        async def cancel_run(self, _run_id: str) -> bool:
            assert self.native_task is not None
            self.native_task.cancel()
            return True

        async def _publish_ack(self, _run_id: str) -> None:
            assert terminal_persisted.is_set()
            listeners_stopped.set()

    manager = ProvisioningManager()
    worker = AgentCoreWorker(
        session_maker=session_maker,
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(return_value=manager),
        heartbeat_seconds=60,
    )

    execution = asyncio.create_task(worker._execute_claimed(dispatch))
    await native_started.wait()
    row.stop_requested = True
    result = await execution

    assert native_cancelled.is_set()
    assert result.error_code == "cancelled"
    assert listeners_stopped.is_set()
    mark_finished.assert_awaited_once()
