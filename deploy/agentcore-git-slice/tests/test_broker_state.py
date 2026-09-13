from __future__ import annotations

import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from botocore.exceptions import ClientError

from cubeplex_git_slice.state import BrokerError, TaskStore, canonical, digest


class MemoryS3:
    """Only the external S3 boundary is fake; real TaskStore performs its CAS."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.lock = threading.Lock()
        self.writes: list[str] = []

    @staticmethod
    def failure(code: str, status: int) -> ClientError:
        return ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "test"
        )

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        with self.lock:
            if Key not in self.objects:
                raise self.failure("NoSuchKey", 404)
            raw = self.objects[Key]
            return {"Body": io.BytesIO(raw), "ETag": hashlib.md5(raw).hexdigest()}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> None:
        with self.lock:
            existing = self.objects.get(Key)
            if kwargs.get("IfNoneMatch") == "*" and existing is not None:
                raise self.failure("PreconditionFailed", 412)
            if "IfMatch" in kwargs and (
                existing is None or hashlib.md5(existing).hexdigest() != kwargs["IfMatch"]
            ):
                raise self.failure("PreconditionFailed", 412)
            self.objects[Key] = bytes(Body)
            self.writes.append(Key)


def make_store() -> TaskStore:
    return TaskStore(MemoryS3(), "test", "task")


def test_concurrent_budget_claims_cannot_overspend_or_reset_between_stages() -> None:
    store = make_store()
    maximum = 100

    def claim(index: int) -> str:
        try:
            store.claim_model(f"request-{index}", maximum)
            return "claimed"
        except BrokerError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(140)))
    assert results.count("claimed") == maximum
    assert store.state()["model_calls"] == maximum
    with pytest.raises(BrokerError, match="model_budget_exceeded"):
        store.claim_model("resume-request", maximum)


def test_request_hash_and_terminal_result_are_immutable() -> None:
    store = make_store()
    assert store.begin_request("request", digest({"a": 1}), "model", "work")[0]
    assert not store.begin_request("request", digest({"a": 1}), "model", "work")[0]
    with pytest.raises(BrokerError, match="request_id_conflict"):
        store.begin_request("request", digest({"a": 2}), "model", "work")
    success = {"ok": True, "result": {"commit": "a"}}
    assert store.finish_request("request", success) == success
    assert (
        store.finish_request("request", {"ok": False, "error": {"code": "late_unknown"}}) == success
    )


def test_publication_has_one_owner_and_never_changes_commit() -> None:
    store = make_store()
    with ThreadPoolExecutor(max_workers=4) as pool:
        owners = list(pool.map(lambda _: store.reserve_push("a"), range(8)))
    assert owners.count(True) == 1
    with pytest.raises(BrokerError, match="commit_conflict"):
        store.reserve_push("b")
    store.finish_push("a")
    assert store.reserve_pr("a", intent="same-publication")
    assert not store.reserve_pr("a", intent="same-publication")
    store.finish_pr({"number": 1, "url": "test"})
    store.claim_model("model-1", 20)
    snapshot = {"stage": "work", "head_sha": "a", "result": {"status": "complete"}}
    sha = store.save_snapshot(snapshot)
    assert store.save_snapshot(snapshot) == sha
    with pytest.raises(BrokerError, match="stage_already_completed"):
        store.save_snapshot({**snapshot, "result": {"model_calls": 0}})
    assert store.state()["model_calls"] == 1
    assert store.load_snapshot() == snapshot
    assert store.public_status()["completed_stages"] == {"work": {"status": "complete"}}
    assert all(not key.endswith("manifest.json") for key in store.s3.writes)
    assert json.loads(canonical(snapshot)) == snapshot


def test_rejected_pr_retry_has_one_owner_and_late_failure_cannot_downgrade() -> None:
    store = make_store()
    store.reserve_push("a")
    store.finish_push("a")
    first = store.reserve_pr("a", intent="same-target")
    assert first is not None
    assert store.record_pr_failure(first, {"code": "github_http_403"}, rejected=True)
    with pytest.raises(BrokerError, match="pr_intent_conflict"):
        store.reserve_pr("a", intent="different-target")
    with ThreadPoolExecutor(max_workers=4) as pool:
        attempts = list(pool.map(lambda _: store.reserve_pr("a", intent="same-target"), range(8)))
    owners = [attempt for attempt in attempts if attempt is not None]
    assert len(owners) == 1 and owners[0] != first
    assert not store.record_pr_failure(first, {"code": "github_http_422"}, rejected=True)
    assert store.state()["pr_phase"] == "pending"
    assert store.state()["pr_attempt"] == owners[0]
    store.finish_pr({"number": 1, "url": "same-pr"})
    assert not store.record_pr_failure(owners[0], {"code": "github_http_403"}, rejected=True)
    assert store.state()["pr_phase"] == "created"
    assert store.reserve_pr("a", intent="same-target") is None
