"""SlackPlatform — PlatformConnector implementation for Slack."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger


class SlackPlatform:
    """PlatformConnector for Slack (Socket Mode only)."""

    def parse_inbound(self, raw: dict[str, Any]) -> Any:
        from cubeplex.im.slack.connector import SlackConnector

        connector = SlackConnector()
        return connector.parse_inbound(raw)

    async def build_tailer(
        self, *, run_id: str, queue_item: Any, account: Any, **kwargs: Any
    ) -> Any:
        app = kwargs["app"]
        lock = getattr(app.state, "im_slack_tailer_start_lock", None)
        if lock is None:
            lock = app.state.im_slack_tailer_start_lock = asyncio.Lock()
        # Worker completion and the recovery sweep can observe the same row.
        # Serialize setup (not streaming) so only one task owns its checkpoint.
        async with lock:
            await self._build_tailer(
                run_id=run_id, queue_item=queue_item, account=account, **kwargs
            )

    async def _build_tailer(
        self, *, run_id: str, queue_item: Any, account: Any, **kwargs: Any
    ) -> None:
        from cubeplex.im.outbound import OutboundRunTailer
        from cubeplex.im.slack.connector import SlackConnector
        from cubeplex.im.slack.renderer import SlackOpDispatcher
        from cubeplex.im.types import RenderState

        app = kwargs["app"]
        tasks = getattr(app.state, "im_slack_tailers", None)
        if tasks is None:
            tasks = app.state.im_slack_tailers = {}
            app.state.im_slack_tailer_retries = {}
        active = tasks.get(run_id)
        if active is not None and not active[1].done():
            return
        retries = app.state.im_slack_tailer_retries
        attempts, retry_after = retries.get(run_id, (0, 0.0))
        if time.monotonic() < retry_after:
            return
        gateways: dict[str, Any] = kwargs.get("gateways", {})
        load_secrets = kwargs.get("load_secrets")

        gw = gateways.get(account.id)
        client = gw.client if gw else None
        if client is None:
            raise RuntimeError("slack_tailer_client_unavailable")

        bot_user_id = ""
        if load_secrets is not None:
            secrets = await load_secrets(account)
            bot_user_id = secrets.get("bot_open_id", "")

        thread_ts = queue_item.reply_to_id

        from cubeplex.im.slack.delivery import SlackDeliveryCheckpoint

        validate_lease = kwargs["validate_connection_lease"]

        async def validate_ownership() -> bool:
            return bool(await validate_lease(account.id))

        checkpoint = SlackDeliveryCheckpoint(
            session_maker=kwargs["session_maker"],
            item_id=queue_item.id,
            run_id=run_id,
            validate_ownership=validate_ownership,
        )

        sc = SlackConnector(
            bot_user_id=bot_user_id,
            client=client,
            channel_id=queue_item.channel_id,
            thread_ts=thread_ts,
            delivery=checkpoint,
        )
        cfg = account.config or {}
        state = RenderState(
            bot_name=cfg.get("bot_app_name") or "CubePlex",
            run_id=run_id,
            reply_to_id=queue_item.reply_to_id,
            inbound_message_id=queue_item.inbound_message_id,
            # chat.update is not bound to the strict 1 msg/s postMessage
            # channel limit; 0.45s keeps the typewriter feeling responsive
            # without hammering Slack (was 1.0s — web finished long before
            # Slack caught up). Tailer also coalesces stream_text in a batch.
            stream_interval=0.45,
        )
        op_dispatcher = SlackOpDispatcher(connector=sc, state=state)

        from cubeplex.im.artifacts import IMArtifactDispatcher

        cfg_obj = kwargs.get("config")
        public_base = str(cfg_obj.get("api.public_url", "") or "") if cfg_obj is not None else ""
        artifact_disp = IMArtifactDispatcher(
            connector=sc,
            redis=app.state.redis,
            redis_key_prefix=app.state.redis_key_prefix,
            public_base_url=public_base,
            org_id=account.org_id,
            workspace_id=account.workspace_id,
            conversation_id=queue_item.conversation_id,
            card_state=state.card_state,
            run_id=run_id,
            platform="slack",
            chat_id=queue_item.channel_id,
            reply_to_id=queue_item.reply_to_id,
            supports_inline_image=False,
        )

        shared_mode = False
        _sm = kwargs.get("session_maker")
        if _sm is not None:
            from cubeplex.im.types import is_shared_mode_for_tailer

            shared_mode = await is_shared_mode_for_tailer(
                _sm,
                queue_item.account_id,
                queue_item.channel_id,
                queue_item.conversation_id,
            )

        tailer = OutboundRunTailer(
            redis=app.state.redis,
            key_prefix=app.state.redis_key_prefix,
            run_id=run_id,
            connector=sc,
            state=state,
            dispatcher=op_dispatcher,
            artifact_dispatcher=artifact_disp,
            responder_open_id=queue_item.sender_open_id,
            shared_mode=shared_mode,
            checkpoint=checkpoint,
        )

        async def run() -> None:
            try:
                await tailer.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retries[run_id] = (
                    attempts + 1,
                    time.monotonic() + min(15 * 2 ** min(attempts, 5), 300),
                )
                logger.warning(
                    "[Slack] delivery pending for run {} ({})", run_id, type(exc).__name__
                )
            else:
                retries.pop(run_id, None)

        task = asyncio.create_task(run(), name=f"im-tailer:{run_id}")
        tasks[run_id] = (account.id, task)

        def finished(done: asyncio.Task[None]) -> None:
            if tasks.get(run_id) == (account.id, done):
                tasks.pop(run_id, None)

        task.add_done_callback(finished)

    async def reconcile_tailers(self, *, account: Any, **kwargs: Any) -> None:
        from sqlalchemy import or_, select
        from sqlmodel import col

        from cubeplex.models.im_connector import IMRunQueueItem

        async with kwargs["session_maker"]() as session:
            rows = (
                (
                    await session.execute(
                        select(IMRunQueueItem)
                        .where(
                            col(IMRunQueueItem.account_id) == account.id,
                            col(IMRunQueueItem.status) == "completed",
                            col(IMRunQueueItem.run_id).is_not(None),
                            or_(
                                col(IMRunQueueItem.outbound_state).is_(None),
                                col(IMRunQueueItem.outbound_state)["terminal"]
                                .as_boolean()
                                .is_not(True),
                            ),
                        )
                        .order_by(col(IMRunQueueItem.created_at))
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
        for item in rows:
            await kwargs["on_run_started"](item.run_id, item)

    async def on_account_enabled(self, account: Any, **kwargs: Any) -> None:
        from cubeplex.im.inbound import ingest_inbound_event
        from cubeplex.im.slack.gateway import SlackGateway

        secrets: dict[str, Any] = kwargs.get("secrets", {})
        gateways: dict[str, Any] = kwargs.get("gateways", {})
        session_maker = kwargs.get("session_maker")
        run_manager = kwargs.get("run_manager")
        redis_key_prefix: str = kwargs.get("redis_key_prefix", "")

        bot_token = str(secrets.get("bot_token") or "")
        app_token = str(secrets.get("app_token") or "")
        bot_user_id = str(secrets.get("bot_open_id") or "")
        if not bot_token or not app_token:
            logger.warning("[Slack] skipping account {} — missing tokens", account.id)
            return

        gw = SlackGateway(
            account=account,
            bot_token=bot_token,
            app_token=app_token,
            bot_user_id=bot_user_id,
            ingest=ingest_inbound_event,
            session_maker=session_maker,
            run_manager=run_manager,
            redis_key_prefix=redis_key_prefix,
        )
        await gw.start()
        gateways[account.id] = gw

    async def on_account_disabled(self, account: Any, **kwargs: Any) -> None:
        gateways: dict[str, Any] = kwargs.get("gateways", {})
        gw = gateways.pop(account.id, None)
        if gw is not None:
            await gw.stop()


async def stop_account_tailers(app: Any, account_id: str | None = None) -> None:
    tasks = [
        task
        for owner, task in getattr(app.state, "im_slack_tailers", {}).values()
        if account_id is None or owner == account_id
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        except TimeoutError:
            logger.warning("[Slack] tailer shutdown remains pending for account {}", account_id)
