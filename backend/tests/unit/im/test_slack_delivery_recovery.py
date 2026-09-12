from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from cubeplex.im.outbound import OutboundRunTailer
from cubeplex.im.slack.connector import SlackConnector
from cubeplex.im.slack.delivery import SlackDeliveryCheckpoint, SlackDeliveryPending
from cubeplex.im.slack.renderer import SlackOpDispatcher
from cubeplex.im.types import RenderState
from cubeplex.streams.run_events import RunEvent


def dispatcher() -> tuple[SlackOpDispatcher, RenderState, MagicMock]:
    state = RenderState(bot_name="bot", run_id="run-1")
    connector = MagicMock()
    connector.send_message = AsyncMock(return_value="1234567890.000002")
    connector.edit_message = AsyncMock(return_value=True)
    connector.add_reaction = AsyncMock()
    connector.remove_reaction = AsyncMock()
    return SlackOpDispatcher(connector=connector, state=state), state, connector


@pytest.mark.asyncio
async def test_unknown_final_edit_never_posts_a_second_message() -> None:
    render, state, connector = dispatcher()
    state.bot_message_id = "1234567890.000002"
    state.card_state.streaming_content = "Completed answer"
    connector.edit_message.side_effect = TimeoutError("response was lost")
    try:
        await render.dispatch_finalize(state)
    except TimeoutError:
        pass
    connector.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_final_edit_is_not_delivery_success() -> None:
    render, state, connector = dispatcher()
    state.bot_message_id = "1234567890.000002"
    state.card_state.streaming_content = "Completed answer"
    connector.edit_message.return_value = False
    assert await render.dispatch_finalize(state) is False


@pytest.mark.asyncio
async def test_failed_create_does_not_commit_split_offset() -> None:
    render, state, connector = dispatcher()
    state.card_state.streaming_content = "x" * 5000
    connector.send_message.return_value = None
    assert await render.dispatch_create(state) is False
    assert render.sent_char_offset == 0


class MemoryCheckpoint(SlackDeliveryCheckpoint):
    def __init__(self, db: dict[str, Any]) -> None:
        super().__init__(
            session_maker=None,
            item_id="queue",
            run_id="run-1",
            validate_ownership=AsyncMock(return_value=True),
        )
        self.db = db

    async def _read(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.db))

    async def _update(
        self, *, post: Any = None, reserve_post: bool = False, **values: Any
    ) -> dict[str, Any] | None:
        await self.assert_owned()
        self.db.update(json.loads(json.dumps(values)))
        if post:
            existing = self.db.get("posts", {}).get(post[0])
            if reserve_post and existing and existing.get("status") != "rejected":
                return dict(existing)
            self.db.setdefault("posts", {})[post[0]] = json.loads(json.dumps(post[1]))
        return None


class SlackBoundary:
    def __init__(self, *, fail_post_number: int = 1) -> None:
        self.messages: list[dict[str, Any]] = []
        self.posts = 0
        self.readback_available = False
        self.fail_post_number = fail_post_number
        self.edits: list[str] = []

    async def chat_postMessage(self, **payload: Any) -> dict[str, Any]:
        self.posts += 1
        message = {**payload, "user": "UBOT", "ts": f"1234567890.{self.posts:06d}"}
        self.messages.append(message)
        if self.posts == self.fail_post_number:
            raise TimeoutError("accepted but response lost")
        return {"ok": True, "channel": "C1", "ts": message["ts"]}

    async def conversations_replies(self, **payload: Any) -> dict[str, Any]:
        assert payload["channel"] == "C1"
        assert payload["ts"] == "1234567800.000001"
        if not self.readback_available:
            raise TimeoutError("readback unavailable")
        return {"ok": True, "messages": self.messages}

    async def chat_update(self, **payload: Any) -> dict[str, Any]:
        self.edits.append(payload["ts"])
        next(m for m in self.messages if m["ts"] == payload["ts"])["text"] = payload["text"]
        return {"ok": True}


def make_tailer(db: dict[str, Any], slack: SlackBoundary) -> tuple[OutboundRunTailer, RenderState]:
    checkpoint = MemoryCheckpoint(db)
    connector = SlackConnector(
        bot_user_id="UBOT",
        client=slack,
        channel_id="C1",
        thread_ts="1234567800.000001",
        delivery=checkpoint,
    )
    state = RenderState(bot_name="bot", run_id="run-1")
    tailer = OutboundRunTailer(
        redis=None,
        key_prefix="test",
        run_id="run-1",
        connector=connector,
        state=state,
        dispatcher=SlackOpDispatcher(connector=connector, state=state),
        checkpoint=checkpoint,
    )
    return tailer, state


@pytest.mark.asyncio
@pytest.mark.parametrize("text,fail_post", [("Hello world", 1), ("x" * 6500, 2)])
async def test_restart_recovers_unknown_post_without_reposting(
    monkeypatch: pytest.MonkeyPatch, text: str, fail_post: int
) -> None:
    db: dict[str, Any] = {}
    slack = SlackBoundary(fail_post_number=fail_post)
    events = [
        RunEvent("1-0", {"type": "text_delta", "data": {"content": text}}),
        RunEvent("2-0", {"type": "done", "data": {}}),
    ]

    async def read(*args: Any, **kwargs: Any) -> list[RunEvent]:
        return [event for event in events if event.event_id > kwargs["last_event_id"]]

    monkeypatch.setattr("cubeplex.im.outbound.read_run_events_after", read)
    tailer, _ = make_tailer(db, slack)
    with pytest.raises(SlackDeliveryPending):
        await tailer.run()
    assert "cursor" not in db
    accepted = slack.posts
    slack.readback_available = True
    restarted, state = make_tailer(db, slack)
    await restarted.run()
    assert db["terminal"] is True
    assert db["cursor"] == "2-0"
    assert state.card_state.streaming_content == text
    assert len({m["client_msg_id"] for m in slack.messages}) == slack.posts
    assert slack.posts == (1 if len(text) < 3000 else 3)
    assert accepted == fail_post
    assert "".join(m["text"] for m in slack.messages) == text
    # Reconstructing the terminal delivery is a no-op.
    await make_tailer(db, slack)[0].run()
    assert slack.posts == (1 if len(text) < 3000 else 3)


@pytest.mark.asyncio
async def test_restore_cursor_preserves_fold_and_existing_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db: dict[str, Any] = {}
    slack = SlackBoundary(fail_post_number=0)
    first, first_state = make_tailer(db, slack)
    first_state.card_state.streaming_content = "Hello "
    await first._dispatcher.dispatch_create(first_state)
    await first._checkpoint.save("1-0", first_state, first._dispatcher)

    async def read(*args: Any, **kwargs: Any) -> list[RunEvent]:
        assert kwargs["last_event_id"] == "1-0"
        return [
            RunEvent("2-0", {"type": "text_delta", "data": {"content": "world"}}),
            RunEvent("3-0", {"type": "done", "data": {}}),
        ]

    monkeypatch.setattr("cubeplex.im.outbound.read_run_events_after", read)
    restarted, state = make_tailer(db, slack)
    original_card = state.card_state
    await restarted.run()
    assert state.card_state is original_card  # artifact dispatcher holds this same object
    assert state.card_state.streaming_content == "Hello world"
    assert slack.messages[0]["text"] == "Hello world"
    assert slack.posts == 1


@pytest.mark.asyncio
async def test_unknown_post_never_matches_another_sender_or_thread() -> None:
    db: dict[str, Any] = {}
    slack = SlackBoundary()
    checkpoint = MemoryCheckpoint(db)
    payload = {"channel": "C1", "thread_ts": "1234567800.000001", "text": "answer"}
    with pytest.raises(SlackDeliveryPending):
        await checkpoint.post(
            key="text:initial:0", client=slack, payload=payload, bot_user_id="UBOT"
        )
    slack.readback_available = True
    slack.messages[0]["user"] = "OTHER"
    with pytest.raises(SlackDeliveryPending):
        await MemoryCheckpoint(db).post(
            key="text:initial:0", client=slack, payload=payload, bot_user_id="UBOT"
        )
    slack.messages[0]["user"] = "UBOT"
    slack.messages[0]["thread_ts"] = "1234567800.000999"
    with pytest.raises(SlackDeliveryPending):
        await MemoryCheckpoint(db).post(
            key="text:initial:0", client=slack, payload=payload, bot_user_id="UBOT"
        )
    assert slack.posts == 1


@pytest.mark.asyncio
async def test_slack_internal_error_is_unknown_not_permission_to_repost() -> None:
    class InternalErrorSlack(SlackBoundary):
        async def chat_postMessage(self, **payload: Any) -> dict[str, Any]:
            result = await super().chat_postMessage(**payload)
            if self.posts == 1:
                raise SlackApiError("internal_error", {"ok": False, "error": "internal_error"})
            return result

    db: dict[str, Any] = {}
    slack = InternalErrorSlack(fail_post_number=0)
    payload = {"channel": "C1", "thread_ts": "1234567800.000001", "text": "answer"}
    with pytest.raises(SlackDeliveryPending):
        await MemoryCheckpoint(db).post(
            key="text:initial:0", client=slack, payload=payload, bot_user_id="UBOT"
        )
    slack.readback_available = True
    await MemoryCheckpoint(db).post(
        key="text:initial:0", client=slack, payload=payload, bot_user_id="UBOT"
    )
    assert slack.posts == 1
