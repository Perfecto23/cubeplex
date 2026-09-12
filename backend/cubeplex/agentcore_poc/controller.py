"""Single-worker Slack polling ingress; intentionally leaves the existing app routing intact."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import boto3
from botocore.config import Config
from dotenv import dotenv_values

from cubeplex.agentcore_poc.contracts import (
    InvocationRequest,
    InvocationResponse,
    derive_runtime_session_id,
)
from cubeplex.agentcore_poc.slack import (
    JsonObject,
    SlackAPI,
    SlackClient,
    SlackError,
    read_messages,
    verify_identity,
)

TEAM_ID = "T011CF3CMJN"
CHANNEL_ID = "C0BKH447YGN"
USER_ID = "U09USS444UE"
BOT_USER_ID = "U0BGHFW9L1F"
REPOSITORY = "Perfecto23/corplink-rs"
PREFIX = "cubeplex-poc:"
_TIMESTAMP = re.compile(r"^\d{10,}\.\d{6}$")


@dataclass(frozen=True)
class PollScope:
    start_ts: str
    team_id: str = TEAM_ID
    channel_id: str = CHANNEL_ID
    user_id: str = USER_ID
    bot_user_id: str = BOT_USER_ID

    def __post_init__(self) -> None:
        if not _TIMESTAMP.fullmatch(self.start_ts):
            raise ValueError("invalid_start_timestamp")
        if (self.team_id, self.channel_id, self.user_id, self.bot_user_id) != (
            TEAM_ID,
            CHANNEL_ID,
            USER_ID,
            BOT_USER_ID,
        ):
            raise ValueError("poc_scope_not_allowed")

    def request(self, message: Mapping[str, Any]) -> InvocationRequest | None:
        text = message.get("text")
        ts = message.get("ts")
        if (
            message.get("user") != self.user_id
            or message.get("user") == self.bot_user_id
            or message.get("team", self.team_id) != self.team_id
            or message.get("channel", self.channel_id) != self.channel_id
            or message.get("subtype") in {"message_changed", "message_deleted"}
            or not isinstance(text, str)
            or not text.startswith(PREFIX)
            or not isinstance(ts, str)
            or not _TIMESTAMP.fullmatch(ts)
            or Decimal(ts) < Decimal(self.start_ts)
        ):
            return None
        thread_ts = message.get("thread_ts") or ts
        if not isinstance(thread_ts, str) or not _TIMESTAMP.fullmatch(thread_ts):
            return None
        prompt = text[len(PREFIX) :].strip()
        if not prompt or len(prompt) > 12_000:
            return None
        return InvocationRequest.model_validate(
            {
                "schema_version": "1",
                "run_id": str(uuid4()),
                "team_id": self.team_id,
                "channel_id": self.channel_id,
                "thread_ts": thread_ts,
                "user_id": self.user_id,
                "prompt": prompt,
                "repository": REPOSITORY,
            }
        )


class RuntimeInvoker(Protocol):
    def invoke(self, request: InvocationRequest, session_id: str) -> InvocationResponse: ...


class AgentCoreInvoker:
    def __init__(self, runtime_arn: str, *, profile: str = "moego-testing") -> None:
        if not re.fullmatch(
            r"arn:aws:bedrock-agentcore:us-west-2:986420599013:runtime/"
            r"cubeplex_poc_20260912[A-Za-z0-9_-]*",
            runtime_arn,
        ):
            raise ValueError("invalid_runtime_arn")
        if profile != "moego-testing":
            raise ValueError("aws_profile_not_allowed")
        self.runtime_arn = runtime_arn
        session = boto3.Session(profile_name=profile, region_name="us-west-2")
        self.client = session.client(
            "bedrock-agentcore",
            config=Config(
                connect_timeout=10,
                read_timeout=310,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )

    def invoke(self, request: InvocationRequest, session_id: str) -> InvocationResponse:
        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=request.model_dump_json().encode(),
        )
        body = response["response"]
        try:
            raw = body.read(2_000_001)
        finally:
            body.close()
        if len(raw) > 2_000_000:
            raise ValueError("runtime_response_too_large")
        return InvocationResponse.model_validate_json(raw)


class Ledger:
    def __init__(self, path: Path) -> None:
        worktree = Path(__file__).resolve().parents[3]
        if not path.is_absolute() or path.resolve().is_relative_to(worktree):
            raise ValueError("ledger_must_be_outside_worktree")
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("ledger_symlink_not_allowed")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.stat().st_mode & 0o077:
            raise ValueError("ledger_directory_must_be_private")
        self._lock = path.with_suffix(path.suffix + ".lock").open("a+")
        os.chmod(self._lock.name, 0o600)
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ValueError("controller_already_running") from None
        previous_umask = os.umask(0o077)
        try:
            self.db = sqlite3.connect(path)
        finally:
            os.umask(previous_umask)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            request TEXT NOT NULL, session_id TEXT NOT NULL,
            result TEXT, reply_ts TEXT, updated_at REAL NOT NULL
        )""")
        self.db.commit()

    def close(self) -> None:
        self.db.close()
        self._lock.close()

    def get(self, event_id: str) -> JsonObject | None:
        row = self.db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def remember(self, event_id: str, request: InvocationRequest, session_id: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO events VALUES (?, 'seen', ?, ?, NULL, NULL, ?)",
                (event_id, request.model_dump_json(), session_id, time.time()),
            )

    def update(
        self, event_id: str, status: str, *, result: str | None = None, reply_ts: str | None = None
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE events SET status=?, result=COALESCE(?, result), "
                "reply_ts=COALESCE(?, reply_ts), updated_at=? WHERE event_id=?",
                (status, result, reply_ts, time.time(), event_id),
            )


def delivery_marker(event_id: str) -> str:
    return "cubeplex-poc-delivery-" + hashlib.sha256(event_id.encode()).hexdigest()[:24]


def format_reply(result: InvocationResponse, event_id: str) -> str:
    label = "CubePlex × AgentCore PoC"
    if result.status == "completed":
        # Sandbox paths are not accessible from Slack; source URLs come from verified evidence.
        text = re.sub(
            r"(?<!!)\[([^\]\n]+)\]\(<?(?:/|file://)[^)\n]*>?\)",
            r"\1",
            result.answer,
        )
        if result.commit_sha:
            text += f"\n\n代码版本：{result.repository}@{result.commit_sha}"
        if result.evidence and result.commit_sha:
            citations = []
            for item in result.evidence[:8]:
                citations.append(
                    f"https://github.com/{result.repository}/blob/{result.commit_sha}/"
                    f"{item.path}#L{item.start_line}-L{item.end_line}"
                )
            text += "\n\n源码证据：\n" + "\n".join(citations)
    else:
        text = f"本次运行未完成。状态：{result.status}；错误：{result.error_code or 'unspecified'}"
    # Do not permit model-generated mentions to notify unrelated users or channels.
    text = text.replace("<@", "&lt;@").replace("<!", "&lt;!")
    message = f"{label}\n\n{text}\n\n[{delivery_marker(event_id)}]"
    if len(message) > 35_000:
        raise ValueError("slack_answer_too_large")
    return message


class PollingController:
    def __init__(
        self,
        scope: PollScope,
        bot: SlackAPI,
        reader: SlackAPI,
        runtime: RuntimeInvoker,
        ledger: Ledger,
    ) -> None:
        self.scope = scope
        self.bot = bot
        self.reader = reader
        self.runtime = runtime
        self.ledger = ledger

    def readback(self, event_id: str, request: InvocationRequest) -> str | None:
        messages = read_messages(
            self.reader,
            channel_id=self.scope.channel_id,
            oldest=request.thread_ts,
            thread_ts=request.thread_ts,
        )
        marker = delivery_marker(event_id)
        matches = [
            m
            for m in messages
            if (
                m.get("user") == self.scope.bot_user_id
                and m.get("thread_ts") == request.thread_ts
                and marker in str(m.get("text", ""))
            )
        ]
        if len(matches) > 1:
            raise ValueError("duplicate_delivery_detected")
        if matches:
            ts = matches[0].get("ts")
            if isinstance(ts, str) and _TIMESTAMP.fullmatch(ts):
                self.ledger.update(event_id, "posted", reply_ts=ts)
                return ts
        return None

    def process(self, message: Mapping[str, Any]) -> JsonObject | None:
        candidate = self.scope.request(message)
        if candidate is None:
            return None
        event_id = f"poll:{self.scope.channel_id}:{message['ts']}"
        self.ledger.remember(event_id, candidate, derive_runtime_session_id(candidate))
        row = self.ledger.get(event_id)
        assert row is not None
        request = InvocationRequest.model_validate_json(row["request"])
        session_id = str(row["session_id"])
        status = row["status"]
        if status == "posted":
            return {"event_id": event_id, "status": "duplicate", "reply_ts": row["reply_ts"]}
        if status in {"posting", "send_unknown"}:
            stored_result = InvocationResponse.model_validate_json(row["result"])
            try:
                ts = self.readback(event_id, request)
            except SlackError:
                ts = None
            return {
                "event_id": event_id,
                "status": "posted" if ts else "send_unknown",
                "run_id": str(request.run_id),
                "runtime_session_id": session_id,
                "runtime_status": stored_result.status,
                "reply_ts": ts,
            }
        if status == "invoking":
            return {"event_id": event_id, "status": "invoke_unknown"}
        if status == "seen":
            self.ledger.update(event_id, "invoking")
            # If transport fails, preserve invoking: a later poll must not repeat the run.
            result = self.runtime.invoke(request, session_id)
            if (
                str(result.run_id) != str(request.run_id)
                or result.runtime_session_id != session_id
                or result.repository != request.repository
            ):
                raise ValueError("runtime_response_identity_mismatch")
            if result.status == "completed" and (
                not result.answer.strip()
                or not result.evidence
                or not re.fullmatch(r"[0-9a-f]{40}", result.commit_sha or "")
            ):
                raise ValueError("runtime_completed_without_evidence")
            self.ledger.update(event_id, "completed", result=result.model_dump_json())
        else:
            result = InvocationResponse.model_validate_json(row["result"])
        text = format_reply(result, event_id)
        # Verify root still exists so Slack cannot silently downgrade a missing thread.
        root = read_messages(
            self.reader,
            channel_id=self.scope.channel_id,
            oldest=request.thread_ts,
            thread_ts=request.thread_ts,
        )
        if not any(m.get("ts") == request.thread_ts for m in root):
            raise ValueError("slack_thread_missing")
        self.ledger.update(event_id, "posting")
        try:
            sent = self.bot.call(
                "chat.postMessage",
                {
                    "channel": self.scope.channel_id,
                    "thread_ts": request.thread_ts,
                    "text": text,
                    "unfurl_links": False,
                    "unfurl_media": False,
                    "reply_broadcast": False,
                    "parse": "none",
                },
            )
            ts = sent.get("ts")
            if sent.get("channel") != self.scope.channel_id or not isinstance(ts, str):
                raise SlackError("slack_invalid_send_response")
            self.ledger.update(event_id, "posting", reply_ts=ts)
        except SlackError:
            self.ledger.update(event_id, "send_unknown")
            return {"event_id": event_id, "status": "send_unknown"}
        try:
            verified_ts = self.readback(event_id, request)
        except SlackError:
            verified_ts = None
        return {
            "event_id": event_id,
            "run_id": str(request.run_id),
            "runtime_session_id": session_id,
            "runtime_status": result.status,
            "status": "posted" if verified_ts else "send_unknown",
            "reply_ts": verified_ts,
        }

    def poll(self, *, max_runs: int) -> list[JsonObject]:
        messages = read_messages(
            self.bot, channel_id=self.scope.channel_id, oldest=self.scope.start_ts
        )
        results: list[JsonObject] = []
        for message in sorted(messages, key=lambda item: str(item.get("ts", ""))):
            result = self.process(message)
            if result:
                results.append(result)
                if (
                    result["status"] != "duplicate"
                    and sum(row["status"] != "duplicate" for row in results) >= max_runs
                ):
                    break
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--start-ts", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-runs", type=int, default=1)
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--poll-interval", type=float, default=4)
    args = parser.parse_args()
    if not (
        1 <= args.max_runs <= 10 and 1 <= args.duration <= 3600 and 3 <= args.poll_interval <= 5
    ):
        parser.error("require max-runs 1..10, duration 1..3600, poll-interval 3..5")
    config = dotenv_values(args.env_file, interpolate=False)
    bot = SlackClient(config.get("SLACK_BOT_TOKEN") or "")
    reader = SlackClient(config.get("SLACK_USER_TOKEN") or "")
    verify_identity(bot, team_id=TEAM_ID, user_id=BOT_USER_ID, require_bot=True)
    verify_identity(reader, team_id=TEAM_ID, user_id=USER_ID, require_bot=False)
    ledger = Ledger(args.ledger)
    try:
        controller = PollingController(
            PollScope(args.start_ts),
            bot,
            reader,
            AgentCoreInvoker(args.runtime_arn),
            ledger,
        )
        deadline = time.monotonic() + args.duration
        handled: set[str] = set()
        unresolved = False
        print(
            json.dumps(
                {
                    "status": "polling",
                    "transport": "slack_history_polling",
                    "channel_id": CHANNEL_ID,
                }
            ),
            flush=True,
        )
        while time.monotonic() < deadline and len(handled) < args.max_runs:
            delay = args.poll_interval
            try:
                results = controller.poll(max_runs=args.max_runs - len(handled))
                for result in results:
                    if result["status"] != "duplicate":
                        handled.add(result["event_id"])
                        unresolved |= (
                            result["status"] != "posted"
                            or result.get("runtime_status") != "completed"
                        )
                        print(json.dumps(result), flush=True)
            except SlackError as exc:
                if not exc.retry_after:
                    raise
                delay = max(delay, exc.retry_after)
            if args.once or len(handled) >= args.max_runs:
                break
            remaining = deadline - time.monotonic()
            if delay >= remaining:
                break
            time.sleep(delay)
        return 2 if unresolved else 0
    finally:
        ledger.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "controller_error", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
