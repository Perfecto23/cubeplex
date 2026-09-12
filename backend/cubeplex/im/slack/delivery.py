"""Slack delivery checkpoints on the existing durable IM queue row."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, fields
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter
from slack_sdk.errors import SlackApiError
from sqlalchemy import select

from cubeplex.im.card_model import CardState
from cubeplex.im.types import RenderState
from cubeplex.models.im_connector import IMRunQueueItem


class SlackDeliveryPending(RuntimeError):
    """Delivery is unconfirmed; retain the checkpoint and never restart the run."""


class SlackDeliveryCheckpoint:
    def __init__(
        self,
        *,
        session_maker: Any,
        item_id: str,
        run_id: str,
        validate_ownership: Callable[[], Awaitable[bool]],
    ) -> None:
        self._session_maker = session_maker
        self._item_id = item_id
        self._run_id = run_id
        self._validate_ownership = validate_ownership
        self.terminal = False

    async def assert_owned(self) -> None:
        if not await self._validate_ownership():
            raise SlackDeliveryPending("slack_delivery_lease_lost")

    async def _read(self) -> dict[str, Any]:
        async with self._session_maker() as session:
            row = await session.get(IMRunQueueItem, self._item_id)
            if row is None or row.run_id != self._run_id:
                raise SlackDeliveryPending("slack_delivery_identity_missing")
            return dict(row.outbound_state or {})

    async def _update(
        self,
        *,
        post: tuple[str, dict[str, Any]] | None = None,
        reserve_post: bool = False,
        **values: Any,
    ) -> dict[str, Any] | None:
        await self.assert_owned()
        async with self._session_maker() as session:
            row = (
                await session.execute(
                    select(IMRunQueueItem)
                    .where(IMRunQueueItem.id == self._item_id)  # type: ignore[arg-type]
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.run_id != self._run_id:
                raise SlackDeliveryPending("slack_delivery_identity_missing")
            snapshot = dict(row.outbound_state or {})
            snapshot.update(values)
            snapshot["schema_version"] = 1
            if post is not None:
                posts = dict(snapshot.get("posts") or {})
                existing = posts.get(post[0])
                if reserve_post and existing and existing.get("status") != "rejected":
                    return dict(existing)
                posts[post[0]] = post[1]
                snapshot["posts"] = posts
            row.outbound_state = snapshot
            session.add(row)
            await session.commit()
        return None

    async def restore(self, state: RenderState, dispatcher: Any) -> str:
        saved = await self._read()
        self.terminal = saved.get("terminal") is True
        render = saved.get("render")
        if render:
            if render.get("run_id") != self._run_id:
                raise SlackDeliveryPending("slack_checkpoint_run_mismatch")
            for field in fields(state):
                if field.name in render and field.name != "card_state":
                    setattr(state, field.name, render[field.name])
            card = TypeAdapter(CardState).validate_python(render["card_state"])
            for field in fields(card):
                setattr(state.card_state, field.name, getattr(card, field.name))
            # Monotonic clocks are process-local; do not reuse their old origin.
            state.last_stream_monotonic = state.last_patch_monotonic = 0
            state.card_state.run_start_monotonic = 0
            dispatcher.restore_checkpoint(saved.get("dispatcher") or {})
        return str(saved.get("cursor") or "0")

    async def save(
        self, cursor: str, state: RenderState, dispatcher: Any, *, terminal: bool = False
    ) -> None:
        await self._update(
            cursor=cursor,
            render=asdict(state),
            dispatcher=dispatcher.checkpoint_state(),
            terminal=terminal,
        )
        self.terminal = terminal

    async def post(
        self, *, key: str, client: Any, payload: dict[str, Any], bot_user_id: str
    ) -> str:
        await self.assert_owned()
        delivery_id = str(uuid5(NAMESPACE_URL, f"cubeplex:slack:{self._run_id}:{key}"))
        existing = ((await self._read()).get("posts") or {}).get(delivery_id)
        if existing and existing.get("ts"):
            return str(existing["ts"])
        if existing and existing.get("status") == "posting":
            return await self._recover_post(client, payload, bot_user_id, delivery_id, existing)

        record = {"status": "posting", "oldest": f"{time.time() - 60:.6f}"}
        existing = await self._update(post=(delivery_id, record), reserve_post=True)
        if existing is not None:
            if existing.get("ts"):
                return str(existing["ts"])
            return await self._recover_post(client, payload, bot_user_id, delivery_id, existing)
        request = dict(payload)
        request["client_msg_id"] = delivery_id
        request["metadata"] = {
            "event_type": "cubeplex_run_delivery",
            "event_payload": {"run_id": self._run_id, "delivery_id": delivery_id},
        }
        # Slack SDK's connection retry can replay an accepted POST. This client
        # is owned by the gateway; only explicit readback may recover an unknown.
        client.retry_handlers = []
        await self.assert_owned()
        try:
            response = await client.chat_postMessage(**request)
        except SlackApiError as exc:
            # Internal/server errors can occur after acceptance. Only explicit
            # validation/auth/rate rejections authorize another POST attempt.
            if exc.response.get("error") in {
                "ratelimited",
                "invalid_auth",
                "not_authed",
                "token_revoked",
                "account_inactive",
                "missing_scope",
                "channel_not_found",
                "not_in_channel",
                "is_archived",
                "msg_too_long",
                "no_text",
                "invalid_blocks",
                "invalid_arguments",
            }:
                await self._update(post=(delivery_id, {**record, "status": "rejected"}))
                raise SlackDeliveryPending("slack_post_rejected") from None
            return await self._recover_post(client, payload, bot_user_id, delivery_id, record)
        except Exception:
            return await self._recover_post(client, payload, bot_user_id, delivery_id, record)
        ts = response.get("ts")
        if (
            response.get("ok") is not True
            or response.get("channel") != payload["channel"]
            or not isinstance(ts, str)
            or re.fullmatch(r"[0-9]+\.[0-9]+", ts) is None
        ):
            return await self._recover_post(client, payload, bot_user_id, delivery_id, record)
        await self._update(post=(delivery_id, {**record, "status": "sent", "ts": ts}))
        return ts

    async def _recover_post(
        self,
        client: Any,
        payload: dict[str, Any],
        bot_user_id: str,
        delivery_id: str,
        record: dict[str, Any],
    ) -> str:
        await self.assert_owned()
        thread_ts = payload.get("thread_ts")
        params = {
            "channel": payload["channel"],
            "oldest": record["oldest"],
            "inclusive": True,
            "limit": 100,
            "include_all_metadata": True,
        }
        if thread_ts:
            params["ts"] = thread_ts
        matches: list[str] = []
        seen_cursors: set[str] = set()
        try:
            for _ in range(5):
                response = await (
                    client.conversations_replies(**params)
                    if thread_ts
                    else client.conversations_history(**params)
                )
                if response.get("ok") is not True:
                    raise SlackDeliveryPending("slack_readback_unavailable")
                for message in response.get("messages", []):
                    metadata = message.get("metadata") or {}
                    marker = metadata.get("event_payload") or {}
                    if (
                        message.get("user") == bot_user_id
                        and message.get("thread_ts") == thread_ts
                        and (
                            message.get("client_msg_id") == delivery_id
                            or (
                                metadata.get("event_type") == "cubeplex_run_delivery"
                                and marker.get("run_id") == self._run_id
                                and marker.get("delivery_id") == delivery_id
                            )
                        )
                        and isinstance(message.get("ts"), str)
                    ):
                        matches.append(message["ts"])
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    if response.get("has_more"):
                        raise SlackDeliveryPending("slack_readback_incomplete")
                    break
                if cursor in seen_cursors:
                    raise SlackDeliveryPending("slack_readback_incomplete")
                seen_cursors.add(cursor)
                params["cursor"] = cursor
            else:
                raise SlackDeliveryPending("slack_readback_incomplete")
        except SlackDeliveryPending:
            raise
        except Exception:
            raise SlackDeliveryPending("slack_readback_unavailable") from None
        if len(matches) != 1:
            raise SlackDeliveryPending("slack_post_outcome_unknown")
        await self._update(post=(delivery_id, {**record, "status": "sent", "ts": matches[0]}))
        return matches[0]
