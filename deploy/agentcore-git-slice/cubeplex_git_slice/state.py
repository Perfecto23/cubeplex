"""Conditional S3 records for one bounded broker task; never writes its manifest."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError


class BrokerError(Exception):
    """Only this fixed, non-sensitive code may cross the broker boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BrokerError("invalid_json") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class TaskStore:
    def __init__(self, s3: Any, bucket: str, task_id: str) -> None:
        self.s3, self.bucket = s3, bucket
        self.prefix = f"tasks/{task_id}"

    def _read(self, suffix: str, limit: int = 6 * 1024 * 1024) -> tuple[dict[str, Any], str] | None:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}/{suffix}")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise BrokerError("state_unavailable") from exc
        body = response["Body"]
        try:
            raw = body.read(limit + 1)
        finally:
            body.close()
        if len(raw) > limit:
            raise BrokerError("state_invalid")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise BrokerError("state_invalid") from exc
        if not isinstance(value, dict):
            raise BrokerError("state_invalid")
        return value, response["ETag"]

    def _write(self, suffix: str, value: dict[str, Any], etag: str | None) -> bool:
        if suffix == "manifest.json":
            raise BrokerError("manifest_read_only")
        condition = {"IfMatch": etag} if etag is not None else {"IfNoneMatch": "*"}
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=f"{self.prefix}/{suffix}",
                Body=canonical(value),
                ContentType="application/json",
                **condition,
            )
        except ClientError as exc:
            # S3 exposes these HTTP conditional-write failures as ClientError.
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in {409, 412}:
                return False
            raise BrokerError("state_unavailable") from exc
        return True

    def manifest(self) -> dict[str, Any]:
        record = self._read("manifest.json", 65536)
        if record is None:
            raise BrokerError("task_not_found")
        return record[0]

    def state(self) -> dict[str, Any]:
        record = self._read("state/state.json")
        return record[0] if record else {"model_calls": 0, "completed_stages": {}}

    def mutate(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        for _ in range(20):
            record = self._read("state/state.json")
            old = record[0] if record else {"model_calls": 0, "completed_stages": {}}
            new = copy.deepcopy(old)
            result = change(new)
            if new == old:
                return result
            if self._write("state/state.json", new, record[1] if record else None):
                return result
        raise BrokerError("state_busy")

    def begin_request(
        self, request_id: str, fingerprint: str, op: str, stage: str
    ) -> tuple[bool, dict[str, Any]]:
        suffix = f"requests/{request_id}.json"
        value = {"hash": fingerprint, "op": op, "stage": stage, "status": "pending"}
        if self._write(suffix, value, None):
            return True, value
        record = self._read(suffix)
        if record is None:
            raise BrokerError("state_busy")
        if record[0].get("hash") != fingerprint:
            raise BrokerError("request_id_conflict")
        return False, record[0]

    def finish_request(self, request_id: str, response: dict[str, Any]) -> dict[str, Any]:
        suffix = f"requests/{request_id}.json"
        for _ in range(20):
            record = self._read(suffix)
            if record is None:
                raise BrokerError("state_invalid")
            row, etag = record
            if row["status"] == "done":
                return row["response"]
            # A readback may resolve an unknown write. Errors cannot overwrite
            # a success, and a pending model call is never submitted again.
            if row["status"] == "failed":
                return row["response"]
            code = response.get("error", {}).get("code", "")
            row["status"] = (
                "done" if response["ok"] else "unknown" if code.endswith("_unknown") else "failed"
            )
            row["response"] = response
            if self._write(suffix, row, etag):
                return response
        raise BrokerError("state_busy")

    def claim_model(self, request_id: str, maximum: int) -> None:
        def change(state: dict[str, Any]) -> None:
            claims = state.setdefault("model_claims", [])
            if request_id in claims:
                raise BrokerError("model_outcome_unknown")
            if state["model_calls"] >= maximum:
                raise BrokerError("model_budget_exceeded")
            claims.append(request_id)
            state["model_calls"] += 1

        self.mutate(change)

    def reserve_push(self, commit: str) -> bool:
        def change(state: dict[str, Any]) -> bool:
            existing = state.get("commit")
            if existing is not None and existing != commit:
                raise BrokerError("commit_conflict")
            if existing is not None:
                return False
            state.update(commit=commit, push_phase="pending")
            return True

        return bool(self.mutate(change))

    def finish_push(self, commit: str) -> None:
        def change(state: dict[str, Any]) -> None:
            if state.get("commit") != commit:
                raise BrokerError("commit_conflict")
            state["push_phase"] = "pushed"

        self.mutate(change)

    def reserve_pr(self, commit: str, *, intent: str) -> str | None:
        attempt = uuid4().hex

        def change(state: dict[str, Any]) -> str | None:
            if state.get("commit") != commit or state.get("push_phase") != "pushed":
                raise BrokerError("commit_not_pushed")
            phase = state.get("pr_phase")
            # Existing pending records (including pre-diagnostic live rows)
            # remain fenced. Only a confirmed rejected attempt can be retried.
            if phase in {"pending", "created"} or state.get("pr") is not None:
                return None
            if phase not in {None, "rejected"}:
                raise BrokerError("state_invalid")
            if state.get("pr_intent") not in {None, intent}:
                raise BrokerError("pr_intent_conflict")
            if phase == "rejected" and state.get("pr_intent") != intent:
                raise BrokerError("pr_intent_conflict")
            state.update(pr_phase="pending", pr_intent=intent, pr_attempt=attempt)
            state.pop("pr_diagnostic", None)
            return attempt

        return self.mutate(change)

    def record_pr_failure(
        self, attempt: str, diagnostic: dict[str, str], *, rejected: bool
    ) -> bool:
        def change(state: dict[str, Any]) -> bool:
            if (
                state.get("pr_phase") != "pending"
                or state.get("pr_attempt") != attempt
                or state.get("pr") is not None
            ):
                return False
            state["pr_diagnostic"] = dict(diagnostic)
            if rejected:
                state["pr_phase"] = "rejected"
            return True

        return bool(self.mutate(change))

    def finish_pr(self, pr: dict[str, Any]) -> None:
        def change(state: dict[str, Any]) -> None:
            if state.get("pr") is not None and state["pr"] != pr:
                raise BrokerError("pr_conflict")
            state.update(pr=pr, pr_phase="created")

        self.mutate(change)

    def save_snapshot(self, snapshot: dict[str, Any]) -> str:
        sha = digest(snapshot)
        suffix = f"snapshots/{sha}.json"
        if not self._write(suffix, snapshot, None):
            existing = self._read(suffix)
            if existing is None or digest(existing[0]) != sha:
                raise BrokerError("snapshot_conflict")

        def change(state: dict[str, Any]) -> None:
            stage = snapshot["stage"]
            completed = state.setdefault("completed_stages", {})
            hashes = state.setdefault("stage_snapshots", {})
            if stage in hashes:
                if hashes[stage] != sha:
                    raise BrokerError("stage_already_completed")
                return
            if state.get("push_phase") != "pushed" or state.get("pr") is None:
                raise BrokerError("publication_incomplete")
            if state.get("commit") != snapshot["head_sha"]:
                raise BrokerError("snapshot_head_mismatch")
            hashes[stage] = sha
            completed[stage] = snapshot["result"]
            state["snapshot_sha256"] = sha

        self.mutate(change)
        return sha

    def load_snapshot(self) -> dict[str, Any]:
        sha = self.state().get("snapshot_sha256")
        if not sha:
            raise BrokerError("snapshot_not_found")
        record = self._read(f"snapshots/{sha}.json")
        if record is None or digest(record[0]) != sha:
            raise BrokerError("snapshot_invalid")
        return record[0]

    def public_status(self) -> dict[str, Any]:
        state = self.state()
        return {
            "commit": state.get("commit") if state.get("push_phase") == "pushed" else None,
            "pr": state.get("pr"),
            "model_calls": state["model_calls"],
            "snapshot_sha256": state.get("snapshot_sha256"),
            "completed_stages": state["completed_stages"],
        }
