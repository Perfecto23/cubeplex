"""Postgres races for the AgentCore dispatch terminal-state fence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cubeplex.agentcore.dispatch import (
    DispatchValidationError,
    agentcore_session_id,
    mark_dispatch_finished,
    mark_dispatch_stop_unknown,
)
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch
from cubeplex.models.conversation import Conversation
from tests.e2e.conftest import _build_database_url
from tests.e2e.im_fixtures import im_cleanup, im_seed_org_ws_user

pytestmark = pytest.mark.asyncio

_ORG_ID = "org-acas01"
_WS_ID = "ws-acas01"
_USER_ID = "usr-acas01"
_CONV_ID = "conv-acas01"


@pytest_asyncio.fixture
async def session_maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_build_database_url(), poolclass=NullPool)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await im_seed_org_ws_user(
            session,
            org_id=_ORG_ID,
            ws_id=_WS_ID,
            user_id=_USER_ID,
            email="agentcore-cas@example.com",
        )
        session.add(
            Conversation(
                id=_CONV_ID,
                org_id=_ORG_ID,
                workspace_id=_WS_ID,
                creator_user_id=_USER_ID,
                title="AgentCore CAS",
                reasoning={},
                attributes={},
            )
        )
        await session.commit()
    try:
        yield maker
    finally:
        async with maker() as session:
            await session.execute(
                text("DELETE FROM agentcore_dispatches WHERE org_id = :org"),
                {"org": _ORG_ID},
            )
            await session.execute(
                text("DELETE FROM conversations WHERE id = :conversation"),
                {"conversation": _CONV_ID},
            )
            await im_cleanup(
                session,
                ws_ids=[_WS_ID],
                user_ids=[_USER_ID],
                org_ids=[_ORG_ID],
            )
            await session.commit()
        await engine.dispose()


async def _seed_dispatch(
    session_maker: async_sessionmaker[AsyncSession],
    run_id: str,
) -> AgentCoreDispatch:
    dispatch = AgentCoreDispatch(
        org_id=_ORG_ID,
        workspace_id=_WS_ID,
        conversation_id=_CONV_ID,
        user_id=_USER_ID,
        run_id=run_id,
        operation="prompt",
        request={"context": {"conversation_id": _CONV_ID}},
        status="claimed",
        session_id="",
    )
    dispatch.session_id = agentcore_session_id(dispatch.id)
    async with session_maker() as session:
        session.add(dispatch)
        await session.commit()
        await session.refresh(dispatch)
    return dispatch


async def _read_dispatch(
    session_maker: async_sessionmaker[AsyncSession],
    dispatch_id: object,
) -> AgentCoreDispatch:
    async with session_maker() as session:
        row = await session.get(AgentCoreDispatch, dispatch_id)
        assert row is not None
        return row


async def test_terminal_writers_preserve_first_finished_or_unknown_state(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    finished_first = await _seed_dispatch(session_maker, "ac-cas-finished-first")
    assert await mark_dispatch_finished(
        session_maker,
        dispatch_id=finished_first.id,
        error_code="first_finished",
        error_message="finished won",
    )
    assert not await mark_dispatch_stop_unknown(
        session_maker,
        dispatch_id=finished_first.id,
        error_message="late unknown must not overwrite",
    )
    row = await _read_dispatch(session_maker, finished_first.id)
    assert row.status == "finished"
    assert row.error_code == "first_finished"
    assert row.error_message == "finished won"

    unknown_first = await _seed_dispatch(session_maker, "ac-cas-unknown-first")
    assert await mark_dispatch_stop_unknown(
        session_maker,
        dispatch_id=unknown_first.id,
        error_message="first unknown",
    )
    assert not await mark_dispatch_finished(
        session_maker,
        dispatch_id=unknown_first.id,
        error_code="late_finished",
        error_message="must remain fenced",
    )
    row = await _read_dispatch(session_maker, unknown_first.id)
    assert row.status == "stop_unknown"
    assert row.error_code == "stop_unknown"
    assert row.error_message == "first unknown"


async def test_concurrent_terminal_writers_have_one_atomic_winner(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    dispatch = await _seed_dispatch(session_maker, "ac-cas-concurrent")

    finished_result, unknown_result = await asyncio.gather(
        mark_dispatch_finished(
            session_maker,
            dispatch_id=dispatch.id,
            error_code="finished_race",
            error_message="finished race winner",
        ),
        mark_dispatch_stop_unknown(
            session_maker,
            dispatch_id=dispatch.id,
            error_message="unknown race winner",
        ),
    )

    assert (finished_result, unknown_result) in ((True, False), (False, True))
    row = await _read_dispatch(session_maker, dispatch.id)
    assert row.status in {"finished", "stop_unknown"}
    if row.status == "finished":
        assert row.error_code == "finished_race"
        assert row.error_message == "finished race winner"
    else:
        assert row.error_code == "stop_unknown"
        assert row.error_message == "unknown race winner"


async def test_terminal_writer_missing_dispatch_is_rejected(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from uuid import uuid4

    with pytest.raises(DispatchValidationError, match="agentcore_dispatch_not_found"):
        await mark_dispatch_finished(session_maker, dispatch_id=uuid4())
    with pytest.raises(DispatchValidationError, match="agentcore_dispatch_not_found"):
        await mark_dispatch_stop_unknown(session_maker, dispatch_id=uuid4())
