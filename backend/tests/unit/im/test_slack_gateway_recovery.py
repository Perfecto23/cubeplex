from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization.authorize_result import AuthorizeResult
from slack_sdk.socket_mode.request import SocketModeRequest

from cubeplex.im.slack.gateway import SlackGateway


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["enqueued", "duplicate", "error"])
async def test_socket_ack_follows_durable_ingest(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    entered, commit = asyncio.Event(), asyncio.Event()

    async def authorize(**kwargs: Any) -> AuthorizeResult:
        return AuthorizeResult(
            enterprise_id=None, team_id="T1", bot_token="xoxb-test", bot_user_id="UBOT"
        )

    def make_app(**kwargs: Any) -> AsyncApp:
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

    monkeypatch.setattr("slack_bolt.async_app.AsyncApp", make_app)
    monkeypatch.setattr(
        "slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler", Handler
    )
    gateway = SlackGateway(
        account=SimpleNamespace(id="account", workspace_id="workspace"),
        bot_token="xoxb-test",
        app_token="xapp-test",
        bot_user_id="UBOT",
        ingest=None,
        session_maker=None,
        run_manager=None,
        redis_key_prefix="test",
    )

    async def ingest(*args: Any) -> None:
        entered.set()
        await commit.wait()
        if outcome == "error":
            raise RuntimeError("transaction failed")

    monkeypatch.setattr(gateway, "_handle_inbound", ingest)
    await gateway.start()
    client = SimpleNamespace(
        send_socket_mode_response=AsyncMock(), logger=logging.getLogger("test")
    )
    request = SocketModeRequest(
        type="events_api",
        envelope_id="envelope",
        payload={
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "text": "hello",
                "ts": "1234567890.000001",
            },
        },
    )
    task = asyncio.create_task(AsyncSocketModeHandler.handle(gateway._handler, client, request))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert client.send_socket_mode_response.await_count == 0
        commit.set()
        await asyncio.wait_for(task, 1)
        assert client.send_socket_mode_response.await_count == (0 if outcome == "error" else 1)
    finally:
        commit.set()
        await gateway.stop()
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_owned_dead_gateway_reconnects_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cubeplex.im import runtime
    from tests.unit.im.test_gateway_lease import FakeRedis

    clock = [100.0]
    monkeypatch.setattr(runtime, "_monotonic", lambda: clock[0], raising=False)
    account = SimpleNamespace(id="a1", credential_id="cred", org_id="org", platform="slack")

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def execute(self, stmt: Any) -> Any:
            accounts = [] if "teams" in stmt.compile().params.values() else [account]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: accounts))

    class Gateway:
        open = True

        def is_open(self) -> bool:
            return self.open

        async def stop(self) -> None:
            self.open = False

    class Platform:
        attempts = 0
        fail = False

        async def on_account_enabled(self, account: Any, **kwargs: Any) -> None:
            self.attempts += 1
            if self.fail:
                raise RuntimeError("offline")
            kwargs["gateways"][account.id] = Gateway()

    platform = Platform()
    monkeypatch.setattr(runtime, "async_session_maker", Session)
    monkeypatch.setattr(
        runtime,
        "build_credential_service",
        lambda *a, **k: SimpleNamespace(get_decrypted=AsyncMock(return_value="{}")),
    )
    monkeypatch.setattr(runtime.IMRunQueueWorker, "start", lambda self: None)
    monkeypatch.setattr("cubeplex.im.registry.get_platform", lambda name: platform)
    app = SimpleNamespace(
        state=SimpleNamespace(redis=FakeRedis(), redis_key_prefix="test", encryption_backend=None)
    )
    await runtime.start(app, SimpleNamespace())
    try:
        await app.state.im_reconcile_connections()
        app.state.im_gateways["a1"].open = False
        await app.state.im_reconcile_connections()
        assert platform.attempts == 2
        assert app.state.im_gateways["a1"].is_open()
        platform.fail = True
        app.state.im_gateways["a1"].open = False
        clock[0] += 60
        await app.state.im_reconcile_connections()
        attempts = platform.attempts
        for _ in range(5):
            await app.state.im_reconcile_connections()
        assert platform.attempts == attempts
        clock[0] += 300
        await app.state.im_reconcile_connections()
        assert platform.attempts == attempts + 1
    finally:
        await runtime.stop(app)
