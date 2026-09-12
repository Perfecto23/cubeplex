"""Protect dispatch scope, at-most-once side effects, and original-thread delivery."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from cubeplex.agentcore_poc.contracts import InvocationRequest, InvocationResponse
from cubeplex.agentcore_poc.controller import (
    BOT_USER_ID,
    CHANNEL_ID,
    REPOSITORY,
    TEAM_ID,
    USER_ID,
    AgentCoreInvoker,
    Ledger,
    PollingController,
    PollScope,
    format_reply,
)
from cubeplex.agentcore_poc.slack import SlackError

ROOT_TS = "1789200000.000001"
MESSAGE_TS = "1789200001.000001"
REPLY_TS = "1789200002.000001"


class SlackBoundary:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = [{"ts": ROOT_TS, "user": USER_ID}]
        self.sends: list[dict[str, object]] = []
        self.unknown_send = False
        self.accept_before_disconnect = False

    def call(self, method: str, payload: Mapping[str, object]) -> dict[str, Any]:
        if method == "conversations.replies":
            assert payload["channel"] == CHANNEL_ID
            assert payload["ts"] == ROOT_TS
            return {"ok": True, "messages": self.messages}
        if method == "chat.postMessage":
            self.sends.append(dict(payload))
            if self.accept_before_disconnect or not self.unknown_send:
                self.messages.append(
                    {
                        "ts": REPLY_TS,
                        "user": BOT_USER_ID,
                        "thread_ts": payload["thread_ts"],
                        "text": payload["text"],
                    }
                )
            if self.unknown_send:
                raise SlackError("slack_transport_unknown")
            return {"ok": True, "ts": REPLY_TS, "channel": CHANNEL_ID}
        raise AssertionError(method)


class RuntimeBoundary:
    def __init__(self) -> None:
        self.requests: list[InvocationRequest] = []
        self.status = "completed"
        self.fail_transport = False
        self.wrong_identity = False

    def invoke(self, request: InvocationRequest, session_id: str) -> InvocationResponse:
        self.requests.append(request)
        if self.fail_transport:
            raise TimeoutError("external transport")
        return InvocationResponse.model_validate(
            {
                "schema_version": "1",
                "run_id": request.run_id,
                "runtime_session_id": "wrong-session"
                if self.wrong_identity
                else session_id,
                "status": self.status,
                "repository": REPOSITORY,
                "answer": "仓库结论。<@UOTHER> <!channel>"
                if self.status == "completed"
                else "",
                "commit_sha": "a" * 40 if self.status == "completed" else None,
                "evidence": [
                    {
                        "path": "README.md",
                        "start_line": 1,
                        "end_line": 2,
                        "blob_sha": "b" * 40,
                        "excerpt": "Example repository",
                    }
                ]
                if self.status == "completed"
                else [],
                "model": "test",
                "error_code": None,
            }
        )


@pytest.fixture
def pipeline(tmp_path: Path) -> Any:
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    slack = SlackBoundary()
    runtime = RuntimeBoundary()
    controller = PollingController(PollScope(ROOT_TS), slack, slack, runtime, ledger)
    yield controller, slack, runtime, ledger
    ledger.close()


def request_message(**changes: object) -> dict[str, object]:
    message: dict[str, object] = {
        "ts": MESSAGE_TS,
        "thread_ts": ROOT_TS,
        "user": USER_ID,
        "channel": CHANNEL_ID,
        "team": TEAM_ID,
        "text": "cubeplex-poc: 分析代码",
    }
    message.update(changes)
    return message


@pytest.mark.parametrize(
    "changes",
    [
        {"user": "UOTHER000001"},
        {"user": BOT_USER_ID},
        {"channel": "COTHER000001"},
        {"team": "TOTHER000001"},
        {"text": "ordinary conversation"},
        {"ts": "1789199999.000001"},
        {"thread_ts": "malformed"},
        {"text": "cubeplex-poc: "},
        {"text": "cubeplex-poc: " + "x" * 12001},
        {"subtype": "message_changed"},
    ],
)
def test_out_of_scope_message_has_no_side_effects(
    pipeline: Any, changes: dict[str, object]
) -> None:
    controller, slack, runtime, _ = pipeline
    assert controller.process(request_message(**changes)) is None
    assert runtime.requests == []
    assert slack.sends == []


def test_reply_is_bot_in_original_thread_and_duplicate_never_reinvokes(
    pipeline: Any,
) -> None:
    controller, slack, runtime, ledger = pipeline
    # A user-token-generated request can carry app metadata; exact author scope wins.
    message = request_message(bot_id="BUSERAPP", app_id="ATESTAPP")
    first = controller.process(message)
    second = controller.process(message)
    assert first["status"] == "posted"
    assert second["status"] == "duplicate"
    assert len(runtime.requests) == len(slack.sends) == 1
    assert runtime.requests[0].repository == REPOSITORY
    assert runtime.requests[0].prompt == "分析代码"
    sent = slack.sends[0]
    assert sent["channel"] == CHANNEL_ID
    assert sent["thread_ts"] == ROOT_TS
    assert sent["reply_broadcast"] is False
    assert str(sent["text"]).startswith("CubePlex × AgentCore PoC")
    assert "<@" not in str(sent["text"])
    assert "<!channel>" not in str(sent["text"])
    assert ledger.get(first["event_id"])["reply_ts"] == REPLY_TS


def test_unknown_send_is_only_read_back_never_resent(pipeline: Any) -> None:
    controller, slack, runtime, ledger = pipeline
    slack.unknown_send = True
    first = controller.process(request_message())
    assert first["status"] == "send_unknown"
    for _ in range(3):
        assert controller.process(request_message())["status"] == "send_unknown"
    assert len(slack.sends) == len(runtime.requests) == 1
    assert ledger.get(first["event_id"])["status"] == "send_unknown"


def test_accepted_send_with_lost_response_is_recovered_by_marker(pipeline: Any) -> None:
    controller, slack, runtime, _ = pipeline
    slack.unknown_send = True
    slack.accept_before_disconnect = True
    assert controller.process(request_message())["status"] == "send_unknown"
    recovered = controller.process(request_message())
    assert recovered["status"] == "posted"
    assert recovered["reply_ts"] == REPLY_TS
    assert len(slack.sends) == len(runtime.requests) == 1


def test_unknown_invoke_is_not_repeated(pipeline: Any) -> None:
    controller, slack, runtime, _ = pipeline
    runtime.fail_transport = True
    with pytest.raises(TimeoutError):
        controller.process(request_message())
    assert controller.process(request_message())["status"] == "invoke_unknown"
    assert len(runtime.requests) == 1
    assert not slack.sends


def test_scope_mismatched_runtime_answer_is_not_delivered(pipeline: Any) -> None:
    controller, slack, runtime, _ = pipeline
    runtime.wrong_identity = True
    with pytest.raises(ValueError, match="runtime_response_identity_mismatch"):
        controller.process(request_message())
    assert not slack.sends
    assert controller.process(request_message())["status"] == "invoke_unknown"
    assert len(runtime.requests) == 1


def test_incomplete_answer_delivers_clear_failure(pipeline: Any) -> None:
    controller, slack, runtime, _ = pipeline
    runtime.status = "incomplete"
    result = controller.process(request_message())
    assert result["runtime_status"] == "incomplete"
    assert "本次运行未完成" in slack.sends[0]["text"]
    assert "仓库结论" not in slack.sends[0]["text"]


def test_missing_root_does_not_post_to_channel(pipeline: Any) -> None:
    controller, slack, _, ledger = pipeline
    slack.messages.clear()
    with pytest.raises(ValueError, match="slack_thread_missing"):
        controller.process(request_message())
    assert not slack.sends
    row = ledger.get(f"poll:{CHANNEL_ID}:{MESSAGE_TS}")
    assert row["status"] == "completed"


def test_ledger_allows_only_one_active_worker(tmp_path: Path) -> None:
    path = tmp_path / "private" / "ledger.sqlite"
    first = Ledger(path)
    try:
        with pytest.raises(ValueError, match="controller_already_running"):
            Ledger(path)
    finally:
        first.close()


@pytest.mark.parametrize(
    "arn,profile",
    [
        (
            "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/cubeplex_poc_20260912-test",
            "moego-testing",
        ),
        (
            "arn:aws:bedrock-agentcore:us-west-2:986420599013:runtime/unrelated-test",
            "moego-testing",
        ),
        (
            "arn:aws:bedrock-agentcore:us-east-1:986420599013:runtime/cubeplex_poc_20260912-test",
            "moego-testing",
        ),
        (
            "arn:aws:bedrock-agentcore:us-west-2:986420599013:runtime/cubeplex_poc_20260912-test",
            "production",
        ),
    ],
)
def test_runtime_target_rejection_precedes_aws_client(arn: str, profile: str) -> None:
    with pytest.raises(ValueError):
        AgentCoreInvoker(arn, profile=profile)


def test_ledger_restart_does_not_replay_delivered_request(tmp_path: Path) -> None:
    path = tmp_path / "private" / "ledger.sqlite"
    slack = SlackBoundary()
    runtime = RuntimeBoundary()
    first = Ledger(path)
    controller = PollingController(PollScope(ROOT_TS), slack, slack, runtime, first)
    assert controller.process(request_message())["status"] == "posted"
    first.close()
    restarted = Ledger(path)
    try:
        controller = PollingController(
            PollScope(ROOT_TS), slack, slack, runtime, restarted
        )
        assert controller.process(request_message())["status"] == "duplicate"
        assert len(runtime.requests) == len(slack.sends) == 1
    finally:
        restarted.close()


def test_ledger_restart_preserves_unknown_send(tmp_path: Path) -> None:
    path = tmp_path / "private" / "ledger.sqlite"
    slack = SlackBoundary()
    slack.unknown_send = True
    runtime = RuntimeBoundary()
    first = Ledger(path)
    controller = PollingController(PollScope(ROOT_TS), slack, slack, runtime, first)
    assert controller.process(request_message())["status"] == "send_unknown"
    first.close()
    restarted = Ledger(path)
    try:
        controller = PollingController(
            PollScope(ROOT_TS), slack, slack, runtime, restarted
        )
        assert controller.process(request_message())["status"] == "send_unknown"
        assert len(runtime.requests) == len(slack.sends) == 1
    finally:
        restarted.close()


def test_reply_drops_local_file_link_targets_but_keeps_verified_source_links() -> None:
    result = InvocationResponse.model_validate(
        {
            "status": "completed",
            "repository": REPOSITORY,
            "commit_sha": "a" * 40,
            "answer": "见 [入口](/workspace/corplink-rs/src/main.rs:12)、"
            "[说明](</workspace/My File.md>) 和 [文件](file:///workspace/test.rs)。"
            "参考 [上游](https://example.com/docs)。",
            "evidence": [
                {
                    "path": "src/main.rs",
                    "start_line": 12,
                    "end_line": 15,
                    "blob_sha": "b" * 40,
                    "excerpt": "verified source",
                }
            ],
        }
    )
    reply = format_reply(result, "event-test")
    assert "见 入口、说明 和 文件。" in reply
    assert "/workspace/" not in reply
    assert "[上游](https://example.com/docs)" in reply
    assert (
        f"https://github.com/{REPOSITORY}/blob/{'a' * 40}/src/main.rs#L12-L15" in reply
    )


def test_restart_readback_preserves_failed_runtime_status(tmp_path: Path) -> None:
    path = tmp_path / "private" / "ledger.sqlite"
    slack = SlackBoundary()
    slack.unknown_send = True
    slack.accept_before_disconnect = True
    runtime = RuntimeBoundary()
    runtime.status = "incomplete"
    first = Ledger(path)
    controller = PollingController(PollScope(ROOT_TS), slack, slack, runtime, first)
    assert controller.process(request_message())["status"] == "send_unknown"
    expected = runtime.requests[0]
    first.close()
    restarted = Ledger(path)
    try:
        controller = PollingController(
            PollScope(ROOT_TS), slack, slack, runtime, restarted
        )
        recovered = controller.process(request_message())
        assert recovered["status"] == "posted"
        assert recovered["runtime_status"] == "incomplete"
        assert recovered["run_id"] == expected.run_id
        assert recovered["runtime_session_id"]
        assert len(runtime.requests) == len(slack.sends) == 1
        assert "本次运行未完成" in slack.sends[0]["text"]
    finally:
        restarted.close()
