"""A known GitHub rejection can be explicitly retried; unknown writes cannot."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cubeplex_git_slice.git_publication import BRANCH, REPO, GitPublication
from cubeplex_git_slice.state import BrokerError
from test_broker import event, make_broker
from test_broker_state import make_store

COMMIT = "b" * 40
REQUEST_ID = "A1B2:C3D4:E5F6:1234"


class GitHubBoundary:
    def __init__(self, first_status: int, request_id: str = REQUEST_ID) -> None:
        self.first_status = first_status
        self.request_id = request_id
        self.posts = 0
        self.pr_exists = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        if request.method == "POST":
            assert request.url.path == f"/repos/{REPO}/pulls"
            body = json.loads(request.content)
            assert body["head"] == BRANCH and body["base"] == "main"
            self.posts += 1
            if self.posts == 1:
                if self.first_status == 0:
                    raise httpx.ReadError("private-value-in-transport-error", request=request)
                return httpx.Response(
                    self.first_status,
                    json={"message": "private-value-in-error-body"},
                    headers={"x-github-request-id": self.request_id, "x-private": "private-header"},
                )
            self.pr_exists = True
            return httpx.Response(201, json={"number": 1})
        if "/git/ref/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": COMMIT}})
        rows = (
            [
                {
                    "number": 1,
                    "head": {"sha": COMMIT, "ref": BRANCH, "repo": {"full_name": REPO}},
                    "base": {"ref": "main"},
                }
            ]
            if self.pr_exists
            else []
        )
        return httpx.Response(200, json=rows)


def data() -> dict[str, Any]:
    return {
        "repo": REPO,
        "branch": BRANCH,
        "commit": COMMIT,
        "title": "Fix inclusive interval total",
        "body": "Same pushed commit.",
    }


@pytest.mark.parametrize("status", [403, 422])
def test_definite_http_rejection_requires_fresh_request_and_keeps_model_budget(status: int) -> None:
    broker, _ = make_broker()
    api = GitHubBoundary(status)
    broker.publication = GitPublication(
        broker.store, lambda: "fake-token", httpx.Client(transport=httpx.MockTransport(api))
    )
    broker.store.reserve_push(COMMIT)
    broker.store.finish_push(COMMIT)
    for index in range(11):
        broker.store.claim_model(f"used-{index}", 20)
    request = event("pr", data())

    response = broker.handle(request)
    assert response == {"ok": False, "error": {"code": f"github_http_{status}"}}
    state = broker.store.state()
    assert state["pr_phase"] == "rejected"
    assert state["pr_diagnostic"] == {"code": f"github_http_{status}", "request_id": REQUEST_ID}
    assert broker.handle(request) == response
    assert api.posts == 1

    successful = broker.handle(event("pr", data()))
    assert successful["ok"] and successful["result"]["status"] == "created"
    assert api.posts == 2
    assert broker.store.state()["pr_phase"] == "created"
    assert broker.store.state()["model_calls"] == 11
    assert broker.store.state()["commit"] == COMMIT
    assert "private-value" not in json.dumps(state)
    assert "private-header" not in json.dumps(state)


@pytest.mark.parametrize("status", [0, 408, 500, 502])
def test_transport_and_ambiguous_http_stay_fenced(status: int) -> None:
    store = make_store()
    store.reserve_push(COMMIT)
    store.finish_push(COMMIT)
    api = GitHubBoundary(status)
    publication = GitPublication(
        store, lambda: "fake-token", httpx.Client(transport=httpx.MockTransport(api))
    )
    for _ in range(2):
        with pytest.raises(BrokerError, match="pr_outcome_unknown"):
            publication.pr(data())
    assert api.posts == 1
    assert store.state()["pr_phase"] == "pending"


def test_existing_live_style_pending_record_is_not_unlocked_by_new_code() -> None:
    store = make_store()
    store.reserve_push(COMMIT)
    store.finish_push(COMMIT)
    store.mutate(lambda state: state.update(pr_phase="pending", model_calls=11))
    before = store.state()
    api = GitHubBoundary(403)
    publication = GitPublication(
        store, lambda: "fake-token", httpx.Client(transport=httpx.MockTransport(api))
    )
    with pytest.raises(BrokerError, match="pr_outcome_unknown"):
        publication.pr(data())
    assert api.posts == 0 and store.state() == before


def test_only_formatted_request_id_enters_safe_diagnostic() -> None:
    store = make_store()
    store.reserve_push(COMMIT)
    store.finish_push(COMMIT)
    api = GitHubBoundary(403, request_id="private-secret-shaped-header")
    publication = GitPublication(
        store, lambda: "fake-token", httpx.Client(transport=httpx.MockTransport(api))
    )
    with pytest.raises(BrokerError, match="github_http_403"):
        publication.pr(data())
    assert store.state()["pr_diagnostic"] == {"code": "github_http_403"}
    assert "private" not in json.dumps(store.state())


def test_http_422_is_not_assumed_duplicate_without_exact_pr_readback() -> None:
    store = make_store()
    store.reserve_push(COMMIT)
    store.finish_push(COMMIT)
    api = GitHubBoundary(422)

    def became_visible(request: httpx.Request) -> httpx.Response:
        response = api(request)
        if request.method == "POST":
            api.pr_exists = True
        return response

    publication = GitPublication(
        store, lambda: "fake-token", httpx.Client(transport=httpx.MockTransport(became_visible))
    )
    result = publication.pr(data())
    assert result == {
        "number": 1,
        "url": f"https://github.com/{REPO}/pull/1",
        "status": "already_exists",
    }
    assert api.posts == 1 and store.state()["pr_phase"] == "created"
