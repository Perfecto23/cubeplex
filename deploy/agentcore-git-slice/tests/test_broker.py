from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from cubeplex_git_slice.broker import Broker, validate_sse
from cubeplex_git_slice.git_publication import BRANCH, REMOTE, REPO
from cubeplex_git_slice.state import BrokerError, canonical
from test_broker_state import make_store

CAPABILITY = "only-a-local-test-capability"
MODEL_KEY = "private-model-value-must-never-escape"


def manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": "task",
        "repo": REPO,
        "remote_url": REMOTE,
        "base_sha": "a" * 40,
        "branch": BRANCH,
        "model": "fixed-model",
        "allowed_paths": ["intervals.py"],
        "artifact_paths": ["README.md", "continuation.md"],
        "active_stage": "work",
        "capability_sha256": hashlib.sha256(CAPABILITY.encode()).hexdigest(),
        "deadline": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "max_model_calls": 20,
        "max_model_request_bytes": 65536,
        "max_model_output_tokens": 2048,
        "max_bundle_bytes": 2097152,
        "max_snapshot_bytes": 4194304,
        "max_changed_bytes": 65536,
    }


def event(
    op: str = "manifest", data: dict[str, Any] | None = None, **overrides: Any
) -> dict[str, Any]:
    return {
        "version": 1,
        "task_id": "task",
        "stage": "work",
        "capability": CAPABILITY,
        "request_id": str(uuid4()),
        "op": op,
        "data": data or {},
        **overrides,
    }


def model_request(**updates: Any) -> dict[str, Any]:
    return {
        "body": {
            "model": "fixed-model",
            "input": "hi",
            "stream": True,
            "store": False,
            "max_output_tokens": 2048,
            **updates,
        }
    }


def sse(status: str = "completed", kind: str = "response.completed") -> bytes:
    return ("data: " + json.dumps({"type": kind, "response": {"status": status}}) + "\n\n").encode()


class Secrets:
    calls = 0

    def get_secret_value(self, **kwargs: Any) -> dict[str, str]:
        self.calls += 1
        return {
            "SecretString": json.dumps(
                {"api_key": MODEL_KEY, "base_url": "https://model.test/v1", "model": "fixed-model"}
            )
        }


def make_broker(handler: Any = None) -> tuple[Broker, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return (
            handler(request)
            if handler
            else httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse())
        )

    store = make_store()
    store.s3.objects["tasks/task/manifest.json"] = canonical(manifest())
    broker = Broker(
        store=store,
        task_id="task",
        secrets=Secrets(),
        model_secret_arn="model",
        github_secret_arn="github",
        http=httpx.Client(transport=httpx.MockTransport(upstream)),
    )
    return broker, calls


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": 1},
        {"version": True},
        {"task_id": "another"},
        {"capability": "wrong"},
        {"stage": "resume"},
        {"op": "shell"},
        {"request_id": "../state/state.json"},
    ],
)
def test_admission_rejects_before_credentials_or_model(changes: dict[str, Any]) -> None:
    broker, calls = make_broker()
    assert not broker.handle({**event("model", model_request()), **changes})["ok"]
    assert not calls and not broker.secrets.calls
    assert not broker.store.s3.writes


def test_expiry_and_stage_rotation_apply_before_replaying_cached_response() -> None:
    broker, _ = make_broker()
    request = event()
    assert broker.handle(request)["ok"]
    current = manifest()
    current["active_stage"] = "resume"
    current["capability_sha256"] = hashlib.sha256(b"new-stage-capability").hexdigest()
    broker.store.s3.objects["tasks/task/manifest.json"] = canonical(current)
    assert broker.handle(request)["error"]["code"] == "capability_denied"
    current["deadline"] = "2000-01-01T00:00:00Z"
    broker.store.s3.objects["tasks/task/manifest.json"] = canonical(current)
    assert (
        broker.handle(event(stage="resume", capability="new-stage-capability"))["error"]["code"]
        == "capability_expired"
    )


def test_public_manifest_has_exact_probe_constraints_without_authorization_secrets() -> None:
    broker, calls = make_broker()
    configured = {
        **manifest(),
        "max_model_calls": 0,
        "worker_role_arn": "arn:aws:iam::123456789012:role/test-worker",
        "forbidden_value_sha256": {"model_master": "d" * 64, "github_writer": "e" * 64},
        "canary_secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:test-canary",
        "canary_sha256": "f" * 64,
        "api_key": "fake-private-model-value",
        "token": "fake-private-github-value",
        "model_secret_arn": "private-model-reference",
    }
    broker.store.s3.objects["tasks/task/manifest.json"] = canonical(configured)
    response = broker.handle(event())
    assert response["ok"]
    result = response["result"]
    expected = {
        "repo",
        "remote_url",
        "base_sha",
        "branch",
        "model",
        "allowed_paths",
        "artifact_paths",
        "active_stage",
        "deadline",
        "canary_secret_arn",
        "canary_sha256",
        "max_model_calls",
        "max_model_output_tokens",
        "max_model_request_bytes",
        "max_bundle_bytes",
        "max_snapshot_bytes",
        "worker_role_arn",
        "forbidden_value_sha256",
    }
    assert set(result) == expected
    assert result == {key: configured[key] for key in expected}
    assert result["max_model_calls"] == 0
    serialized = json.dumps(response)
    for forbidden in (
        "capability_sha256",
        configured["capability_sha256"],
        CAPABILITY,
        "fake-private-model-value",
        "fake-private-github-value",
        "private-model-reference",
    ):
        assert forbidden not in serialized
    assert not calls and not broker.secrets.calls


@pytest.mark.parametrize(
    "updates",
    [
        {"model": "other"},
        {"background": True},
        {"store": True},
        {"stream": False},
        {"max_output_tokens": 2049},
        {"tools": [{"type": "web_search"}]},
        {"previous_response_id": "another-user"},
        {"input": "x" * 65536},
        {
            "input": [
                {"role": "user", "content": [{"type": "input_file", "file_url": "https://wrong"}]}
            ]
        },
    ],
)
def test_model_scope_and_size_fail_before_budget_or_secret(updates: dict[str, Any]) -> None:
    broker, calls = make_broker()
    assert not broker.handle(event("model", model_request(**updates)))["ok"]
    assert not calls and not broker.secrets.calls
    assert broker.store.public_status()["model_calls"] == 0


def test_budget_zero_and_exact_request_replay() -> None:
    broker, calls = make_broker()
    current = manifest()
    current["max_model_calls"] = 0
    broker.store.s3.objects["tasks/task/manifest.json"] = canonical(current)
    assert (
        broker.handle(event("model", model_request()))["error"]["code"] == "model_budget_exceeded"
    )
    assert not calls and not broker.secrets.calls
    current["max_model_calls"] = 20
    broker.store.s3.objects["tasks/task/manifest.json"] = canonical(current)
    request = event("model", model_request())
    response = broker.handle(request)
    assert response["ok"]
    assert broker.handle(request) == response
    assert len(calls) == 1 and broker.store.public_status()["model_calls"] == 1
    assert MODEL_KEY not in json.dumps(response)
    assert response["result"]["headers"] == {"content-type": "text/event-stream"}
    changed = {**request, "data": model_request(input="different")}
    assert broker.handle(changed)["error"]["code"] == "request_id_conflict"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw", [sse("incomplete"), sse(kind="response.incomplete"), b"data: {}\n\n", sse()[:-2]]
)
def test_incomplete_and_truncated_sse_are_never_success(raw: bytes) -> None:
    with pytest.raises(BrokerError, match="model_response_invalid"):
        validate_sse(raw)


def test_model_error_does_not_leak_credentials_or_resubmit_unknown() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError(MODEL_KEY, request=request)

    broker, calls = make_broker(fail)
    request = event("model", model_request())
    response = broker.handle(request)
    assert response == {"ok": False, "error": {"code": "model_outcome_unknown"}}
    assert not broker.handle(request)["ok"]
    assert len(calls) == 1
    assert MODEL_KEY not in json.dumps(broker.store.state())


def test_early_sse_done_and_upstream_error_text_are_not_returned() -> None:
    broker, _ = make_broker(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n" + sse()
        )
    )
    assert (
        broker.handle(event("model", model_request()))["error"]["code"] == "model_response_invalid"
    )
    failed, _ = make_broker(
        lambda _: httpx.Response(401, text=MODEL_KEY, headers={"x-secret": MODEL_KEY})
    )
    response = failed.handle(event("model", model_request()))
    assert response == {"ok": False, "error": {"code": "model_unavailable"}}
    assert MODEL_KEY not in json.dumps(response)


def test_process_loss_after_budget_claim_does_not_resubmit_model() -> None:
    from cubeplex_git_slice.state import digest

    broker, calls = make_broker()
    request = event("model", model_request())
    broker.store.begin_request(
        request["request_id"],
        digest({key: value for key, value in request.items() if key != "capability"}),
        "model",
        "work",
    )
    broker.store.claim_model(request["request_id"], 20)
    assert broker.handle(request)["error"]["code"] == "request_outcome_unknown"
    assert broker.store.public_status()["model_calls"] == 1
    assert not calls and not broker.secrets.calls


@pytest.mark.parametrize(
    "updates",
    [
        {"repo": "another/repo"},
        {"branch": "main"},
        {"base_sha": "c" * 40},
        {"force": True},
    ],
)
def test_push_scope_is_denied_before_git_or_credentials(updates: dict[str, Any]) -> None:
    broker, calls = make_broker()
    data = {
        "repo": REPO,
        "branch": BRANCH,
        "base_sha": "a" * 40,
        "commit": "b" * 40,
        "bundle_b64": "",
        **updates,
    }
    assert not broker.handle(event("push", data))["ok"]
    assert not calls and not broker.secrets.calls


def test_snapshot_cannot_change_budget_scope_paths_or_completed_result() -> None:
    broker, _ = make_broker()
    commit = "b" * 40
    broker.store.reserve_push(commit)
    broker.store.finish_push(commit)
    broker.store.finish_pr({"number": 1, "url": "https://github.com/fixture/pull/1"})
    broker.store.claim_model("first", 20)

    class BundleBoundary:
        @contextmanager
        def validate_bundle(self, *args: Any):
            yield Path("/unused")

        def validate_patch(self, *args: Any) -> None:
            pass

    broker.publication = BundleBoundary()  # type: ignore[assignment]
    snapshot = {
        "schema_version": 1,
        "stage": "work",
        "boot_id": "boot-1",
        "head_sha": commit,
        "base_sha": "a" * 40,
        "branch": BRANCH,
        "git_bundle_b64": "",
        "patch_b64": "",
        "untracked": {"continuation.md": base64.b64encode(b"resume").decode()},
        "messages": [{"role": "user", "content": []}],
        "metrics": {},
        "result": {"model_calls": 0},
    }
    for key in ("../continuation.md", ".git/config", "README.md", ".hidden"):
        denied = {**snapshot, "untracked": {key: ""}}
        assert (
            broker.handle(event("checkpoint_put", {"snapshot": denied}))["error"]["code"]
            == "artifact_path_denied"
        )
    assert not broker.handle(
        event("checkpoint_put", {"snapshot": {**snapshot, "head_sha": "c" * 40}})
    )["ok"]
    response = broker.handle(event("checkpoint_put", {"snapshot": snapshot}))
    assert response["ok"]
    assert broker.store.public_status()["model_calls"] == 1
    assert broker.store.load_snapshot() == snapshot
    assert (
        broker.handle(event("model", model_request()))["error"]["code"] == "stage_already_completed"
    )
