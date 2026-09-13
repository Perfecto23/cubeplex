"""Bounded Git/native-history snapshots for MicroVM handoff."""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import binascii
from pathlib import Path
from typing import Any, Mapping


class SnapshotError(RuntimeError):
    pass


def _run_git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise SnapshotError(result.stderr.decode(errors="replace")[:512])
    return result.stdout


def _safe_untracked(repo: Path, allowed: set[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for raw in _run_git(repo, "ls-files", "--others", "--exclude-standard").decode().splitlines():
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or raw not in allowed:
            raise SnapshotError("untracked_path_not_allowed")
        data = (repo / path).read_bytes()
        if b".git" in path.parts:
            raise SnapshotError("git_path_in_snapshot")
        files[raw] = base64.b64encode(data).decode()
    return files


def capture_snapshot(
    repo: Path,
    *,
    stage: str,
    boot_id: str,
    base_sha: str,
    branch: str,
    native_messages: list[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    result: Mapping[str, Any],
    artifact_paths: set[str],
    max_snapshot_bytes: int,
) -> dict[str, Any]:
    head_sha = _run_git(repo, "rev-parse", "HEAD").decode().strip()
    bundle_path = repo / ".agentcore-snapshot.bundle"
    try:
        _run_git(repo, "bundle", "create", str(bundle_path), "--all")
        bundle = bundle_path.read_bytes()
    finally:
        bundle_path.unlink(missing_ok=True)
    patch = _run_git(repo, "diff", "--binary", "--", *sorted(artifact_paths))
    snapshot = {
        "schema_version": 1,
        "stage": stage,
        "boot_id": boot_id,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "branch": branch,
        "git_bundle_b64": base64.b64encode(bundle).decode(),
        "patch_b64": base64.b64encode(patch).decode(),
        "untracked": _safe_untracked(repo, {"continuation.md"}),
        "messages": list(native_messages),
        "metrics": dict(metrics),
        "result": dict(result),
    }
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > max_snapshot_bytes:
        raise SnapshotError("snapshot_too_large")
    return snapshot


def restore_snapshot(
    repo: Path, snapshot: Mapping[str, Any], *, branch: str, base_sha: str
) -> None:
    if (
        type(snapshot.get("schema_version")) is not int
        or snapshot.get("schema_version") != 1
        or snapshot.get("base_sha") != base_sha
        or snapshot.get("branch") != branch
    ):
        raise SnapshotError("snapshot_base_mismatch")
    head = str(snapshot.get("head_sha") or "")
    if any(
        len(value) != 40 or any(c not in "0123456789abcdef" for c in value)
        for value in (head, base_sha)
    ):
        raise SnapshotError("snapshot_head_invalid")
    try:
        bundle = base64.b64decode(str(snapshot.get("git_bundle_b64") or ""), validate=True)
        patch = base64.b64decode(str(snapshot.get("patch_b64") or ""), validate=True)
        untracked: dict[str, bytes] = {}
        for raw_path, encoded in dict(snapshot.get("untracked") or {}).items():
            if raw_path != "continuation.md":
                raise SnapshotError("snapshot_path_invalid")
            untracked[raw_path] = base64.b64decode(str(encoded), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SnapshotError("snapshot_encoding_invalid") from exc
    if not bundle or len(bundle) > 2_097_152:
        raise SnapshotError("snapshot_bundle_invalid")
    # Import and verify the saved bundle in an isolated object database first.
    # The public clone need only contain the base; no origin fetch supplies HEAD.
    with tempfile.TemporaryDirectory(prefix="agentcore-restore-") as temporary:
        staging = Path(temporary)
        _run_git(staging, "init", "--bare", ".")
        _run_git(staging, "fetch", "--no-tags", str(repo.resolve()), base_sha)
        bundle_path = staging / "snapshot.bundle"
        bundle_path.write_bytes(bundle)
        _run_git(staging, "bundle", "verify", str(bundle_path))
        _run_git(staging, "fetch", "--no-tags", str(bundle_path), head)
        _run_git(staging, "merge-base", "--is-ancestor", base_sha, head)
        _run_git(repo, "fetch", "--no-tags", str(staging), head)
    _run_git(repo, "checkout", "-B", branch, head)
    _run_git(repo, "reset", "--hard", head)
    if patch:
        subprocess.run(["git", "apply", "--whitespace=nowarn"], cwd=repo, input=patch, check=True)
    for raw_path, content in untracked.items():
        target = repo / raw_path
        if target.is_symlink():
            raise SnapshotError("snapshot_path_invalid")
        target.write_bytes(content)
