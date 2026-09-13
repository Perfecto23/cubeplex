"""RunManager remote-selection and stop contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from cubeplex.streams.run_manager import RunContext, RunManager


def _manager(redis: object, *, agentcore_worker: bool = False) -> RunManager:
    app = SimpleNamespace(state=SimpleNamespace(agentcore_worker=agentcore_worker))
    return RunManager(
        app=app,  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        key_prefix="agentcore-product-test",
        run_event_ttl_seconds=60,
    )


def _ctx() -> RunContext:
    return RunContext(user_id="u1", org_id="o1", workspace_id="w1", conversation_id="c1")


@pytest.mark.asyncio
async def test_remote_execute_persists_dispatch_before_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    monkeypatch.setattr(manager, "_agentcore_remote_enabled", lambda: True)
    dispatch = SimpleNamespace(id="dispatch-1", request={}, status="created")
    create = AsyncMock(return_value=dispatch)
    invoke = AsyncMock()
    monkeypatch.setattr(manager, "_create_remote_prompt_dispatch", create)
    monkeypatch.setattr(manager, "_invoke_remote_dispatch", invoke)

    await manager._execute_run(
        run_id="r1",
        conversation_id="c1",
        content="hello",
        attachments=[],
        ctx=_ctx(),
    )
    create.assert_awaited_once()
    invoke.assert_awaited_once_with(dispatch)


@pytest.mark.asyncio
async def test_remote_stop_unknown_does_not_cancel_control_plane_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    monkeypatch.setattr(manager, "_agentcore_remote_enabled", lambda: True)
    remote = SimpleNamespace(
        id="d1",
        request={},
        run_id="r1",
        heartbeat_at=datetime.now(UTC),
        claimed_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_run",
        AsyncMock(return_value=remote),
    )
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.request_dispatch_stop",
        AsyncMock(return_value=[remote]),
    )
    monkeypatch.setattr(manager, "_publish_control", AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(manager, "_agentcore_client", lambda *_: SimpleNamespace(stop=AsyncMock()))
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.mark_dispatch_stop_unknown",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_run",
        AsyncMock(side_effect=[remote, remote]),
    )

    result = await manager.dispatch_cancel("r1", ack_timeout=0)
    assert result == "stop_unknown"


@pytest.mark.asyncio
async def test_remote_cancel_ack_with_active_dispatch_is_stop_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    monkeypatch.setattr(manager, "_agentcore_remote_enabled", lambda: True)
    remote = SimpleNamespace(id="d1", run_id="r1", request={}, status="claimed")
    unknown = SimpleNamespace(id="d1", run_id="r1", request={}, status="stop_unknown")
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_run",
        AsyncMock(side_effect=[remote, unknown]),
    )
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.request_dispatch_stop",
        AsyncMock(return_value=[remote]),
    )
    mark_unknown = AsyncMock()
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.mark_dispatch_stop_unknown",
        mark_unknown,
    )

    async def publish_and_ack(run_id: str, _type: str) -> None:
        await manager._handle_ack({"run_id": run_id})

    monkeypatch.setattr(manager, "_publish_control", publish_and_ack)

    result = await manager.dispatch_cancel("r1", ack_timeout=1)

    assert result == "stop_unknown"
    mark_unknown.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_cancel_reads_terminal_dispatch_before_runtime_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    monkeypatch.setattr(manager, "_agentcore_remote_enabled", lambda: True)
    remote = SimpleNamespace(id="d1", run_id="r1", request={}, status="claimed")
    stop = AsyncMock()
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_run",
        AsyncMock(side_effect=[remote, None]),
    )
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.request_dispatch_stop",
        AsyncMock(return_value=[remote]),
    )
    monkeypatch.setattr(manager, "_publish_control", AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(manager, "_agentcore_client", lambda *_: SimpleNamespace(stop=stop))

    result = await manager.dispatch_cancel("r1", ack_timeout=0.1)

    assert result == "cancelled"
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_api_control_does_not_cancel_proxy_or_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    monkeypatch.setattr(manager, "_agentcore_remote_enabled", lambda: True)
    manager._tasks["r1"] = asyncio.create_task(asyncio.sleep(10))
    cancel = AsyncMock()
    ack = AsyncMock()
    monkeypatch.setattr(manager, "cancel_run", cancel)
    monkeypatch.setattr(manager, "_publish_ack", ack)

    await manager._handle_control({"run_id": "r1", "type": "cancel"})

    cancel.assert_not_awaited()
    ack.assert_not_awaited()
    manager._tasks["r1"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await manager._tasks["r1"]


@pytest.mark.asyncio
async def test_worker_cancel_returns_after_native_task_without_dispatch_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis, agentcore_worker=True)
    manager._tasks["r1"] = asyncio.create_task(asyncio.sleep(10))
    readback = AsyncMock(return_value=SimpleNamespace(status="stop_unknown"))
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_run",
        readback,
    )

    result = await manager.cancel_run("r1")

    assert result is True
    readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_sandbox_stop_unknown_cas_loss_keeps_unknown_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis, agentcore_worker=True)
    manager._stop_unknown_runs.clear()
    dispatch = SimpleNamespace(id="d1", run_id="r1")
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_run",
        AsyncMock(return_value=dispatch),
    )
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.mark_dispatch_stop_unknown",
        AsyncMock(return_value=False),
    )
    session = MagicMock()
    session.get = AsyncMock(return_value=SimpleNamespace(status="stop_unknown"))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    import importlib

    db_engine = importlib.import_module("cubeplex.db.engine")
    monkeypatch.setattr(db_engine, "async_session_maker", MagicMock(return_value=session))

    await manager._mark_sandbox_stop_unknown(
        run_id="r1",
        conversation_id="c1",
        error=RuntimeError("unknown"),
    )

    assert "r1" in manager._stop_unknown_runs
    session.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_cancel_ack_waits_for_finished_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis, agentcore_worker=True)
    manager._tasks["r1"] = asyncio.create_task(asyncio.sleep(10))
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_run",
        AsyncMock(
            side_effect=[
                SimpleNamespace(status="claimed"),
                None,
            ]
        ),
    )

    result = await manager.cancel_run("r1")

    assert result is True


@pytest.mark.asyncio
async def test_new_prompt_is_blocked_while_remote_stop_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    monkeypatch.setattr(manager, "_agentcore_remote_enabled", lambda: True)
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_conversation",
        AsyncMock(
            return_value=SimpleNamespace(
                run_id="r1",
                org_id="o1",
                workspace_id="w1",
                user_id="u1",
                status="stop_unknown",
            )
        ),
    )

    with pytest.raises(RuntimeError, match="teardown is unconfirmed"):
        await manager.start_run(
            conversation_id="c1",
            content="must wait",
            ctx=_ctx(),
        )


@pytest.mark.asyncio
async def test_explicit_new_run_id_cannot_bypass_existing_remote_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    monkeypatch.setattr(manager, "_agentcore_remote_enabled", lambda: True)
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_conversation",
        AsyncMock(
            return_value=SimpleNamespace(
                run_id="old-run",
                org_id="o1",
                workspace_id="w1",
                user_id="u1",
                status="claimed",
            )
        ),
    )

    with pytest.raises(RuntimeError, match="already has an active run"):
        await manager.start_run(
            conversation_id="c1",
            content="must not bypass",
            run_id="new-run",
            ctx=_ctx(),
        )


@pytest.mark.asyncio
async def test_remote_reconcile_max_lifetime_ends_with_bounded_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    remote = SimpleNamespace(
        id="d1",
        request={},
        run_id="r1",
        heartbeat_at=datetime.now(UTC),
        claimed_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_run",
        AsyncMock(return_value=remote),
    )
    stop = AsyncMock()
    monkeypatch.setattr(manager, "_agentcore_client", lambda *_: SimpleNamespace(stop=stop))
    mark_unknown = AsyncMock()
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.mark_dispatch_stop_unknown",
        mark_unknown,
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setattr(
        "cubeplex.config.config.get",
        lambda key, default=None: {
            "agentcore.reconcile_interval_seconds": 5.0,
            "agentcore.dispatch_heartbeat_timeout_seconds": 180,
            "agentcore.max_lifetime_seconds": 900,
        }.get(key, default),
    )

    await manager._reconcile_remote_dispatch("r1")

    stop.assert_awaited_once_with("d1")
    mark_unknown.assert_awaited_once()


@pytest.mark.asyncio
async def test_accepted_remote_invoke_attaches_one_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    dispatch = SimpleNamespace(id="d1", run_id="r1", request={}, status="created")
    invoke = AsyncMock(return_value={"status": "accepted", "dispatch_id": "d1"})
    attach = MagicMock()
    monkeypatch.setattr(manager, "_agentcore_client", lambda *_: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(manager, "_ensure_remote_monitor", attach)

    await manager._invoke_remote_dispatch(dispatch)

    invoke.assert_awaited_once_with("d1")
    attach.assert_called_once_with("r1")


@pytest.mark.asyncio
async def test_remote_monitor_registry_coalesces_duplicate_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    started = asyncio.Event()
    release = asyncio.Event()

    async def monitor(_: str) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(manager, "_reconcile_remote_dispatch", monitor)
    first = manager._ensure_remote_monitor("r1")
    second = manager._ensure_remote_monitor("r1")
    assert first is second
    await started.wait()
    release.set()
    await first
    assert manager._remote_monitors == {}


@pytest.mark.asyncio
async def test_restart_attaches_monitors_to_all_active_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = _manager(redis)
    monkeypatch.setattr(manager, "_agentcore_remote_enabled", lambda: True)
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatches",
        AsyncMock(
            return_value=[
                SimpleNamespace(run_id="r1"),
                SimpleNamespace(run_id="r2"),
            ]
        ),
    )
    attach = MagicMock()
    monkeypatch.setattr(manager, "_ensure_remote_monitor", attach)

    await manager.attach_remote_monitors()

    assert [call.args[0] for call in attach.call_args_list] == ["r1", "r2"]
