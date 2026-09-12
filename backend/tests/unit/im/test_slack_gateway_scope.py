from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization.authorize_result import AuthorizeResult
from slack_sdk.socket_mode.request import SocketModeRequest

from cubeplex.config import config
from cubeplex.im.slack.gateway import SlackGateway


@asynccontextmanager
async def gateway(
    monkeypatch: pytest.MonkeyPatch, *, configured: bool = True
) -> AsyncIterator[tuple[SlackGateway, Any, AsyncMock]]:
    original_get = config.get

    def get(key: str, default: Any = None, **kwargs: Any) -> Any:
        if key == "im.slack.allowed_channel_ids":
            return ["C_ALLOWED"] if configured else None
        if key == "im.slack.allowed_user_ids":
            return ["U_ALLOWED"] if configured else None
        return original_get(key, default, **kwargs)

    monkeypatch.setattr(config, "get", get)

    async def authorize(**kwargs: Any) -> AuthorizeResult:
        return AuthorizeResult(
            enterprise_id=None, team_id="T1", bot_token="xoxb-test", bot_user_id="UBOT"
        )

    def app(**kwargs: Any) -> AsyncApp:
        return AsyncApp(
            **kwargs,
            authorize=authorize,
            request_verification_enabled=False,
            ignoring_self_events_enabled=False,
        )

    class Handler:
        def __init__(self, app: Any, token: str) -> None:
            self.app = app

        async def start_async(self) -> None:
            await asyncio.Event().wait()

        async def close_async(self) -> None:
            pass

    effect = AsyncMock()

    def commands(app: Any, **kwargs: Any) -> None:
        @app.command("/link")
        async def link(ack: Any) -> None:
            await ack()
            await effect("link/reply")

    monkeypatch.setattr("slack_bolt.async_app.AsyncApp", app)
    monkeypatch.setattr(
        "slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler", Handler
    )
    monkeypatch.setattr("cubeplex.im.slack.commands.register_commands", commands)
    monkeypatch.setattr("cubeplex.im.slack.interactions.handle_block_action", effect)
    instance = SlackGateway(
        account=SimpleNamespace(id="account", workspace_id="workspace"),
        bot_token="xoxb-test",
        app_token="xapp-test",
        bot_user_id="UBOT",
        ingest=None,
        session_maker=None,
        run_manager=None,
        redis_key_prefix="test",
    )
    monkeypatch.setattr(instance, "_handle_inbound", effect)
    await instance.start()
    instance.client.chat_postMessage = AsyncMock()
    client = SimpleNamespace(
        send_socket_mode_response=AsyncMock(), logger=logging.getLogger("test")
    )
    try:
        yield instance, client, effect
    finally:
        await instance.stop()


def request(kind: str, channel: str, user: str) -> SocketModeRequest:
    if kind in {"app_mention", "message"}:
        payload = {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {
                "type": kind,
                "user": user,
                "channel": channel,
                "channel_type": "im" if kind == "message" else "channel",
                "text": "link person@example.test",
                "ts": "1234567890.000001",
            },
        }
        transport = "events_api"
    elif kind == "action":
        payload = {
            "type": "block_actions",
            "team": {"id": "T1"},
            "user": {"id": user},
            "channel": {"id": channel},
            "actions": [{"action_id": "im:ask_user:r1:q1:k:yes", "type": "button"}],
        }
        transport = "interactive"
    else:
        payload = {
            "team_id": "T1",
            "channel_id": channel,
            "user_id": user,
            "command": "/link",
            "text": "person@example.test",
        }
        transport = "slash_commands"
    return SocketModeRequest(type=transport, envelope_id="envelope", payload=payload)


@pytest.mark.asyncio
async def test_disallowed_channel_or_user_is_acked_without_ingest_or_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with gateway(monkeypatch) as (instance, client, effect):
        for kind in ("app_mention", "message", "action", "slash"):
            for channel, user in (("C_OTHER", "U_ALLOWED"), ("C_ALLOWED", "U_OTHER")):
                client.send_socket_mode_response.reset_mock()
                await AsyncSocketModeHandler.handle(
                    instance._handler, client, request(kind, channel, user)
                )
                client.send_socket_mode_response.assert_awaited_once()
                effect.assert_not_awaited()
                instance.client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_message_action_and_link_keep_the_existing_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with gateway(monkeypatch) as (instance, client, effect):
        for kind in ("app_mention", "message", "action", "slash"):
            effect.reset_mock()
            await AsyncSocketModeHandler.handle(
                instance._handler, client, request(kind, "C_ALLOWED", "U_ALLOWED")
            )
            effect.assert_awaited_once()


@pytest.mark.asyncio
async def test_unconfigured_scope_preserves_upstream_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with gateway(monkeypatch, configured=False) as (instance, client, effect):
        await AsyncSocketModeHandler.handle(
            instance._handler, client, request("app_mention", "C_OTHER", "U_OTHER")
        )
        effect.assert_awaited_once()
