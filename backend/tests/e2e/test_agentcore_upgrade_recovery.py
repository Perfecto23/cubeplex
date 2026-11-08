"""Upstream Stop recovery preserves the remote dispatch's ownership."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from cubeplex.agentcore.dispatch import DispatchScope, DispatchValidationError, create_dispatch
from cubeplex.db.engine import async_session_maker
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch
from cubeplex.models.conversation_execution import ConversationExecutionAdmission
from cubeplex.streams.run_events import _active_run_key, _run_meta_key
from cubeplex.streams.run_manager import RunManager
from tests.e2e.test_agentcore_native_admission import (
    AdmittedTask,
    admitted_native,  # noqa: F401
    claim,
)
from tests.e2e.test_agentcore_native_lifecycle import native_task  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("started", [False, True])
async def test_stop_recovery_routes_remote_owner_without_claim_takeover(
    admitted_native: AdmittedTask,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    started: bool,
) -> None:
    task, admission_id, client = admitted_native
    redis, prefix, dispatch = task
    if started:
        await claim(client)
    before_claim = await redis.hget(_run_meta_key(prefix, dispatch.run_id), "claim_token")
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        admission.run_stop_requested_at = datetime.now(UTC)
        await session.commit()
    manager = RunManager(
        app=SimpleNamespace(state=SimpleNamespace()),
        redis=redis,
        key_prefix=prefix,
        run_event_ttl_seconds=120,
    )
    stop = AsyncMock(return_value={"statusCode": 200})
    monkeypatch.setattr(manager, "_agentcore_remote_enabled", lambda: True)
    monkeypatch.setattr(manager, "_agentcore_client", lambda _: SimpleNamespace(stop=stop))
    local_cleanup = AsyncMock(side_effect=AssertionError("remote claim must not be replaced"))
    monkeypatch.setattr(manager, "_start_paused_cleanup", local_cleanup)
    assert await manager.recover_stopped_run(admission_id)
    stop.assert_awaited_once_with(dispatch.id)
    local_cleanup.assert_not_awaited()
    assert await redis.get(_active_run_key(prefix, dispatch.conversation_id)) is None
    assert await redis.hget(_run_meta_key(prefix, dispatch.run_id), "claim_token") == before_claim
    async with async_session_maker() as session:
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        current = await session.get(AgentCoreDispatch, dispatch.id)
        assert admission is not None and admission.run_finished_at is not None
        assert admission.run_terminal_status == "cancelled"
        assert current is not None and current.status == "finished"


async def test_stop_before_dispatch_insert_denies_new_remote_work(
    admitted_native: AdmittedTask,  # noqa: F811
) -> None:
    (redis, prefix, dispatch), admission_id, _ = admitted_native
    async with async_session_maker() as session:
        current = await session.get(AgentCoreDispatch, dispatch.id)
        assert current is not None
        request = dict(current.request)
        await session.delete(current)
        admission = await session.get(ConversationExecutionAdmission, admission_id)
        assert admission is not None
        admission.run_stop_requested_at = datetime.now(UTC)
        await session.commit()
    with pytest.raises(DispatchValidationError, match="admission_revoked"):
        await create_dispatch(
            async_session_maker,
            scope=DispatchScope(
                org_id=dispatch.org_id,
                workspace_id=dispatch.workspace_id,
                conversation_id=dispatch.conversation_id,
                user_id=dispatch.user_id,
                run_id=dispatch.run_id,
            ),
            operation="prompt",
            request=request,
        )
    async with async_session_maker() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentCoreDispatch)
                .where(AgentCoreDispatch.run_id == dispatch.run_id)
            )
            == 0
        )
