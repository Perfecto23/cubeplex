"""Slack Socket Mode gateway — one slack-bolt AsyncApp per IM account."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger

from cubeplex.im.slack.connector import SlackConnector


def _configured_allowlist(key: str) -> frozenset[str] | None:
    from cubeplex.config import config

    values = config.get(key, None)
    if values is None:
        return None
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise ValueError(f"{key} must be a list of non-empty IDs")
    return frozenset(values)


def _request_origin(body: dict[str, Any]) -> tuple[str | None, str | None]:
    event = body.get("event")
    source = event if isinstance(event, dict) else body
    channel = source.get("channel_id") or source.get("channel")
    user = source.get("user_id") or source.get("user")
    if isinstance(channel, dict):
        channel = channel.get("id")
    if isinstance(user, dict):
        user = user.get("id")
    if not channel and isinstance(body.get("container"), dict):
        channel = body["container"].get("channel_id")
    return (channel if isinstance(channel, str) else None, user if isinstance(user, str) else None)


class SlackGateway:
    """Manages one slack-bolt AsyncApp per IM account."""

    def __init__(
        self,
        *,
        account: Any,
        bot_token: str,
        app_token: str,
        bot_user_id: str,
        ingest: Any,
        session_maker: Any,
        run_manager: Any,
        redis_key_prefix: str,
    ) -> None:
        self._account = account
        self._bot_token = bot_token
        self._app_token = app_token
        self._bot_user_id = bot_user_id
        self._ingest = ingest
        self._session_maker = session_maker
        self._run_manager = run_manager
        self._redis_key_prefix = redis_key_prefix
        self._app: Any = None
        self._task: asyncio.Task[None] | None = None
        self._client: Any = None
        self._handler: Any = None

    async def start(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import (
            AsyncSocketModeHandler,
        )
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.response import BoltResponse

        allowed_channels = _configured_allowlist("im.slack.allowed_channel_ids")
        allowed_users = _configured_allowlist("im.slack.allowed_user_ids")

        async def filter_scope(body: dict[str, Any], next: Any) -> Any:
            channel, user = _request_origin(body)
            if (allowed_channels is not None and channel not in allowed_channels) or (
                allowed_users is not None and user not in allowed_users
            ):
                # Acknowledge and discard before any listener can ingest, link,
                # resume or reply. The same hook covers events/actions/commands.
                return BoltResponse(status=200, body="")
            return await next()

        # Re-apply after slack_sdk import/connect: its socket client logs every
        # PING/PONG at DEBUG when the leaf logger level is NOTSET.
        try:
            from cubeplex.utils.log import _apply_third_party_log_levels

            _apply_third_party_log_levels()
        except Exception:
            logger.opt(exception=True).debug("[Slack] re-apply third-party log levels failed")

        app = AsyncApp(token=self._bot_token, before_authorize=filter_scope)
        self._app = app
        self._client = app.client
        account = self._account
        session_maker = self._session_maker
        ingest = self._ingest
        bot_user_id = self._bot_user_id

        async def commit_before_ack(event: dict[str, Any], next: Any) -> Any:
            # Bolt 1.28 auto-acks events before the listener, but AFTER listener
            # middleware. Keep the existing atomic receipt/queue transaction in
            # this public hook so Socket Mode only acks a committed admission.
            if event.get("type") == "app_mention" or event.get("channel_type") == "im":
                await self._handle_inbound(event, bot_user_id, account, session_maker, ingest)
            return await next()

        @app.event("app_mention", middleware=[commit_before_ack])
        @app.event("message", middleware=[commit_before_ack])
        async def handle_message() -> None:
            pass

        @app.action(re.compile(r"^im:"))
        async def handle_action(ack: Any, action: dict[str, Any], body: dict[str, Any]) -> None:
            await ack()
            from cubeplex.im.slack.interactions import handle_block_action

            await handle_block_action(
                action=action,
                body=body,
                run_manager=self._run_manager,
                redis_key_prefix=self._redis_key_prefix,
            )

        from cubeplex.im.slack.commands import register_commands

        register_commands(
            app,
            account_id=account.id,
            workspace_id=account.workspace_id,
            session_maker=session_maker,
        )

        handler = AsyncSocketModeHandler(app, self._app_token)
        self._handler = handler

        async def _run() -> None:
            try:
                await handler.start_async()  # type: ignore[no-untyped-call]
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Slack] Socket Mode handler crashed for {}", account.id)

        self._task = asyncio.create_task(_run(), name=f"slack-gateway:{account.id}")

        def _on_task_done(task: asyncio.Task[None]) -> None:
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                logger.opt(exception=True).error(
                    "[Slack] gateway task crashed for {}: {}",
                    account.id,
                    exc,
                )

        self._task.add_done_callback(_on_task_done)
        logger.info("[Slack] Gateway started for account {}", account.id)

    async def _handle_inbound(
        self,
        event: dict[str, Any],
        bot_user_id: str,
        account: Any,
        session_maker: Any,
        ingest: Any,
    ) -> None:
        from cubeplex.im.types import lookup_binding_mode

        channel_id = event.get("channel", "")
        binding_mode = await lookup_binding_mode(session_maker, account.id, channel_id)
        connector = SlackConnector(bot_user_id=bot_user_id)
        parsed = connector.parse_inbound(event, binding_mode=binding_mode)
        if parsed is None:
            return
        parsed.account_external_id = account.external_account_id

        from cubeplex.im.reset_command import (
            apply_reset_command,
            format_reset_reply,
            parse_reset_command,
        )
        from cubeplex.im.slack.commands import parse_link_command

        gate_connector = SlackConnector(
            bot_user_id=bot_user_id,
            client=self._client,
            channel_id=parsed.channel_id,
            thread_ts=parsed.reply_to_id,
        )

        # Text-form /link (or bare ``link``) — intercept before identity gate
        # so the user gets a bind URL instead of an agent reply. Needed when
        # the Slack app has no slash-command registration, or the user types
        # the command as a normal DM / @mention message.
        link_email = parse_link_command(parsed.text)
        if link_email is not None:
            try:
                await self._handle_text_link(
                    email=link_email,
                    sender_ref=parsed.sender_ref or parsed.sender_open_id or "",
                    channel_id=parsed.channel_id,
                    reply_to_id=parsed.reply_to_id,
                    account=account,
                    connector=gate_connector,
                )
            except Exception:
                logger.exception("[Slack] /link handler failed for {}", parsed.platform_event_id)
                raise
            return

        if parse_reset_command(parsed.text):
            try:
                outcome = await apply_reset_command(
                    session_maker=session_maker,
                    account_id=account.id,
                    channel_id=parsed.channel_id,
                    scope_key=parsed.scope_key,
                )
                await gate_connector.send_to_chat(
                    parsed.channel_id, parsed.reply_to_id, format_reset_reply(outcome)
                )
            except Exception:
                logger.exception("[Slack] /new handler failed for {}", parsed.platform_event_id)
                raise
            return

        try:
            result = await ingest(
                parsed,
                account=account,
                session_maker=session_maker,
                identity_resolver=gate_connector,
                rejection_notifier=gate_connector,
            )
            logger.info(
                "[Slack] inbound {}: {}",
                parsed.platform_event_id,
                result.outcome,
            )
        except Exception:
            logger.exception("[Slack] ingest failed for {}", parsed.platform_event_id)
            raise

    async def _handle_text_link(
        self,
        *,
        email: str,
        sender_ref: str,
        channel_id: str,
        reply_to_id: str | None,
        account: Any,
        connector: SlackConnector,
    ) -> None:
        """Reply with an identity-link confirmation URL for a text /link command."""
        if not sender_ref:
            await connector.send_to_chat(
                channel_id, reply_to_id, "Could not determine your user ID."
            )
            return
        try:
            from cubeplex.im.link import get_frontend_base_url, get_jwt_secret, sign_link_token

            token = sign_link_token(
                im_user_id=sender_ref,
                email=email,
                account_id=account.id,
                workspace_id=account.workspace_id,
                platform="slack",
                secret=get_jwt_secret(),
            )
        except Exception:
            logger.opt(exception=True).warning("[Slack] sign_link_token failed")
            await connector.send_to_chat(channel_id, reply_to_id, "Failed to generate link.")
            return

        base = get_frontend_base_url()
        url = f"{base}/im-link?token={token}"
        await connector.send_to_chat(
            channel_id,
            reply_to_id,
            f"Click to complete linking:\n{url}",
        )

    async def stop(self) -> None:
        if self._handler is not None:
            try:
                await asyncio.wait_for(self._handler.close_async(), timeout=5)
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("[Slack] Gateway stopped for account {}", self._account.id)

    def is_open(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def client(self) -> Any:
        return self._client
