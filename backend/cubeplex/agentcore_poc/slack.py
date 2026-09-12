"""Bounded Slack Web API transport for the isolated AgentCore proof of concept."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol

JsonObject = dict[str, Any]


class SlackAPI(Protocol):
    def call(self, method: str, payload: Mapping[str, object]) -> JsonObject: ...


class SlackError(RuntimeError):
    """Contains only a stable error code, never remote response text or credentials."""

    def __init__(self, code: str, *, retry_after: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class SlackClient:
    _METHODS = {
        "auth.test",
        "conversations.history",
        "conversations.replies",
        "chat.postMessage",
    }

    def __init__(self, token: str, *, timeout: float = 20) -> None:
        if not token.startswith(("xoxb-", "xoxp-")):
            raise ValueError("slack_token_type_invalid")
        self._token = token
        self.timeout = timeout

    def call(self, method: str, payload: Mapping[str, object]) -> JsonObject:
        if method not in self._METHODS:
            raise ValueError("slack_method_not_allowed")
        request = urllib.request.Request(
            f"https://slack.com/api/{method}",
            data=json.dumps(dict(payload)).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    delay = max(1, int(exc.headers.get("Retry-After", "60")))
                except ValueError:
                    delay = 60
                raise SlackError("slack_rate_limited", retry_after=delay) from None
            raise SlackError("slack_http_error") from None
        except (OSError, TimeoutError):
            raise SlackError("slack_transport_unknown") from None
        if len(raw) > 2_000_000:
            raise SlackError("slack_response_too_large")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SlackError("slack_invalid_response") from None
        if not isinstance(data, dict):
            raise SlackError("slack_invalid_response")
        if data.get("ok") is not True:
            # Slack error strings can contain arbitrary upstream data. Do not echo them.
            raise SlackError("slack_api_rejected")
        return data


def verify_identity(client: SlackAPI, *, team_id: str, user_id: str, require_bot: bool) -> None:
    identity = client.call("auth.test", {})
    if identity.get("team_id") != team_id or identity.get("user_id") != user_id:
        raise ValueError("slack_identity_mismatch")
    if bool(identity.get("bot_id")) != require_bot:
        raise ValueError("slack_actor_type_mismatch")


def read_messages(
    client: SlackAPI,
    *,
    channel_id: str,
    oldest: str,
    thread_ts: str | None = None,
) -> list[JsonObject]:
    method = "conversations.replies" if thread_ts else "conversations.history"
    params: dict[str, object] = {
        "channel": channel_id,
        "oldest": oldest,
        "inclusive": True,
        "limit": 100,
    }
    if thread_ts:
        params["ts"] = thread_ts
    messages: list[JsonObject] = []
    seen_cursors: set[str] = set()
    for _ in range(5):
        data = client.call(method, params)
        page = data.get("messages")
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise SlackError("slack_invalid_messages")
        messages.extend(page)
        metadata = data.get("response_metadata", {})
        cursor = metadata.get("next_cursor", "") if isinstance(metadata, dict) else ""
        if not cursor:
            if data.get("has_more"):
                raise SlackError("slack_incomplete_page")
            return messages
        if not isinstance(cursor, str) or cursor in seen_cursors:
            raise SlackError("slack_invalid_cursor")
        seen_cursors.add(cursor)
        params["cursor"] = cursor
    raise SlackError("slack_page_budget_exceeded")
