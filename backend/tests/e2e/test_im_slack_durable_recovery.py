from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from cubeplex.im.inbound import ingest_inbound_event
from cubeplex.im.slack._platform import SlackPlatform
from cubeplex.im.slack.delivery import SlackDeliveryCheckpoint
from cubeplex.im.slack.renderer import SlackOpDispatcher
from cubeplex.im.types import InboundEvent, RenderState
from cubeplex.im.worker import process_one_queue_item
from cubeplex.models.im_connector import IMRunQueueItem
from tests.e2e.test_im_worker import _FakeRunManager
from tests.e2e.test_im_worker import _seeded as _worker_seed_fixture

pytestmark = pytest.mark.asyncio
_seeded = _worker_seed_fixture


def event(key: str) -> InboundEvent:
    return InboundEvent(
        platform="slack",
        account_external_id="team",
        platform_event_id=key,
        channel_id="C1",
        scope_key="u:U1|t:1234567890.000001",
        scope_kind="thread",
        reply_to_id="1234567890.000001",
        inbound_message_id="1234567890.000002",
        sender_ref="U1",
        sender_open_id="U1",
        text="Read the repository",
    )


async def test_reclaim_after_dispatch_crash_reuses_persisted_run_id(
    _seeded: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cubeplex.im import worker

    maker, account = _seeded
    await ingest_inbound_event(event("crash"), account=account, session_maker=maker)
    accepted: set[str] = set()
    invoked: list[str] = []

    class Runtime:
        async def start_run(self, **kwargs: Any) -> str:
            run_id = kwargs["run_id"]
            async with maker() as session:
                row = (
                    await session.execute(
                        select(IMRunQueueItem).where(IMRunQueueItem.account_id == account.id)
                    )
                ).scalar_one()
                assert row.run_id == run_id  # committed before any runtime side effect
            invoked.append(run_id)
            accepted.add(run_id)  # RunManager's idempotent explicit-ID boundary
            return run_id

    complete = worker.mark_queue_item_completed
    monkeypatch.setattr(
        worker, "mark_queue_item_completed", AsyncMock(side_effect=RuntimeError("crash"))
    )
    callback = AsyncMock()
    with pytest.raises(RuntimeError, match="crash"):
        await process_one_queue_item(
            session_maker=maker, run_manager=Runtime(), on_run_started=callback, lease_seconds=300
        )
    async with maker() as session:
        row = (
            await session.execute(
                select(IMRunQueueItem).where(IMRunQueueItem.account_id == account.id)
            )
        ).scalar_one()
        assert row.status == "started"
        row.claim_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(row)
        await session.commit()
    monkeypatch.setattr(worker, "mark_queue_item_completed", complete)
    await process_one_queue_item(
        session_maker=maker, run_manager=Runtime(), on_run_started=callback, lease_seconds=300
    )
    assert len(invoked) == 2
    assert len(accepted) == 1
    callback.assert_awaited_once()


async def test_completed_queue_restores_only_undelivered_tailer(_seeded: Any) -> None:
    maker, account = _seeded
    await ingest_inbound_event(event("tailer"), account=account, session_maker=maker)
    await process_one_queue_item(
        session_maker=maker,
        run_manager=_FakeRunManager(),
        on_run_started=AsyncMock(side_effect=RuntimeError("tailer setup failed")),
        lease_seconds=300,
    )
    callback = AsyncMock()
    platform = SlackPlatform()
    await platform.reconcile_tailers(account=account, session_maker=maker, on_run_started=callback)
    callback.assert_awaited_once()
    run_id, row = callback.await_args.args
    checkpoint = SlackDeliveryCheckpoint(
        session_maker=maker,
        item_id=row.id,
        run_id=run_id,
        validate_ownership=AsyncMock(return_value=True),
    )
    state = RenderState(bot_name="bot", run_id=run_id)
    state.card_state.streaming_content = "durable final"
    state.bot_message_id = "1234567890.000010"
    dispatcher = SlackOpDispatcher(connector=None, state=state)
    await checkpoint.save("3-0", state, dispatcher, terminal=True)
    restored = RenderState(bot_name="bot", run_id=run_id)
    restored_dispatcher = SlackOpDispatcher(connector=None, state=restored)
    assert await checkpoint.restore(restored, restored_dispatcher) == "3-0"
    assert restored.bot_message_id == state.bot_message_id
    assert restored.card_state.streaming_content == "durable final"
    callback.reset_mock()
    await platform.reconcile_tailers(account=account, session_maker=maker, on_run_started=callback)
    callback.assert_not_awaited()


async def test_slow_identity_and_duplicate_event_enqueue_once(
    _seeded: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maker, account = _seeded
    entered, release, retry_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    resolutions = 0

    async def resolve(**kwargs: Any) -> str:
        nonlocal resolutions
        resolutions += 1
        entered.set()
        await release.wait()
        return account.acting_user_id

    monkeypatch.setattr("cubeplex.im.inbound.resolve_or_reject", resolve)

    async def ingest(*, retry: bool = False) -> Any:
        if retry:
            retry_started.set()
        return await ingest_inbound_event(
            event("redelivery"),
            account=account,
            session_maker=maker,
            identity_resolver=object(),
            rejection_notifier=object(),
        )

    original = asyncio.create_task(ingest())
    await asyncio.wait_for(entered.wait(), 2)
    retry = asyncio.create_task(ingest(retry=True))
    await asyncio.wait_for(retry_started.wait(), 2)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(original, retry), 5)
    assert {result.outcome for result in results} == {"enqueued", "duplicate"}
    assert resolutions == 1
    async with maker() as session:
        rows = (
            (
                await session.execute(
                    select(IMRunQueueItem).where(IMRunQueueItem.account_id == account.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_concurrent_delivery_claims_make_only_one_slack_post(_seeded: Any) -> None:
    maker, account = _seeded
    await ingest_inbound_event(event("concurrent-post"), account=account, session_maker=maker)
    await process_one_queue_item(
        session_maker=maker, run_manager=_FakeRunManager(), on_run_started=None, lease_seconds=300
    )
    async with maker() as session:
        row = (
            await session.execute(
                select(IMRunQueueItem).where(IMRunQueueItem.account_id == account.id)
            )
        ).scalar_one()
    barrier = asyncio.Barrier(2)

    class ConcurrentCheckpoint(SlackDeliveryCheckpoint):
        async def _read(self) -> dict[str, Any]:
            snapshot = await super()._read()
            await barrier.wait()  # both observe absence before either reserves
            return snapshot

    messages: list[dict[str, Any]] = []

    async def post(**payload: Any) -> dict[str, Any]:
        messages.append({**payload, "user": "UBOT", "ts": "1234567890.000010"})
        return {"ok": True, "channel": "C1", "ts": "1234567890.000010"}

    client = SimpleNamespace(
        chat_postMessage=AsyncMock(side_effect=post),
        conversations_replies=AsyncMock(
            side_effect=lambda **kwargs: {"ok": True, "messages": messages}
        ),
    )
    checkpoints = [
        ConcurrentCheckpoint(
            session_maker=maker,
            item_id=row.id,
            run_id=row.run_id,
            validate_ownership=AsyncMock(return_value=True),
        )
        for _ in range(2)
    ]
    results = await asyncio.wait_for(
        asyncio.gather(
            *[
                cp.post(
                    key="text:initial:0",
                    client=client,
                    bot_user_id="UBOT",
                    payload={
                        "channel": "C1",
                        "thread_ts": "1234567890.000001",
                        "text": "same answer",
                    },
                )
                for cp in checkpoints
            ]
        ),
        5,
    )
    assert results == ["1234567890.000010", "1234567890.000010"]
    client.chat_postMessage.assert_awaited_once()
