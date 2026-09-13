from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cubeplex_git_slice.snapshot import SnapshotError, capture_snapshot, restore_snapshot


def _git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


def test_snapshot_restores_commit_artifact_and_untracked(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "intervals.py").write_text("def total(values): return sum(values)\n")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "README.md").write_text("handoff\n")
    (repo / "continuation.md").write_text("resume\n")
    snapshot = capture_snapshot(
        repo,
        stage="work",
        boot_id="boot",
        base_sha=base,
        branch="agentcore/fix-inclusive-total",
        native_messages=[],
        metrics={},
        result={"status": "complete"},
        artifact_paths={"README.md", "continuation.md"},
        max_snapshot_bytes=4_194_304,
    )
    (repo / "README.md").write_text("changed\n")
    (repo / "continuation.md").unlink()
    restore_snapshot(repo, snapshot, branch="agentcore/fix-inclusive-total", base_sha=base)
    assert (repo / "README.md").read_text() == "handoff\n"
    assert (repo / "continuation.md").read_text() == "resume\n"


def test_fresh_base_only_repo_restores_commit_from_bundle(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.name", "test")
    _git(work, "config", "user.email", "test@example.invalid")
    (work / "intervals.py").write_text("def total(a, b): return b - a\n")
    (work / "README.md").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "base")
    base = _git(work, "rev-parse", "HEAD").stdout.strip()
    fresh = tmp_path / "fresh"
    _git(tmp_path, "clone", "--no-hardlinks", str(work), str(fresh))
    (work / "intervals.py").write_text("def total(a, b): return b - a + 1\n")
    _git(work, "commit", "-qam", "work commit unavailable on cloned remote")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "remote", "add", "origin", "https://invalid.example/never-read")
    (work / "README.md").write_text("handoff README\n")
    (work / "continuation.md").write_text("continue the same commit\n")
    messages = [{"role": "user", "content": [{"type": "text", "text": "fix interval"}]}]
    snapshot = capture_snapshot(
        work,
        stage="work",
        boot_id="boot-1",
        base_sha=base,
        branch="agentcore/fix-inclusive-total",
        native_messages=messages,
        metrics={"first": True},
        result={"commit": head},
        artifact_paths={"README.md", "continuation.md"},
        max_snapshot_bytes=4_194_304,
    )
    missing = subprocess.run(["git", "cat-file", "-e", head], cwd=fresh, capture_output=True)
    assert missing.returncode != 0
    # Prove recovery does not fetch the original remote to obtain work HEAD.
    _git(fresh, "remote", "set-url", "origin", "https://invalid.example/never-read")
    restore_snapshot(fresh, snapshot, branch="agentcore/fix-inclusive-total", base_sha=base)
    assert _git(fresh, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(fresh, "rev-parse", "HEAD^").stdout.strip() == base
    assert (fresh / "intervals.py").read_text() == "def total(a, b): return b - a + 1\n"
    assert (fresh / "README.md").read_text() == "handoff README\n"
    assert (fresh / "continuation.md").read_text() == "continue the same commit\n"
    assert snapshot["messages"] == messages
    assert _git(fresh, "status", "--porcelain").stdout.splitlines() == [
        " M README.md",
        "?? continuation.md",
    ]
    for invalid in ({**snapshot, "git_bundle_b64": ""}, {**snapshot, "branch": "main"}):
        with pytest.raises(SnapshotError):
            restore_snapshot(fresh, invalid, branch="agentcore/fix-inclusive-total", base_sha=base)
        assert _git(fresh, "rev-parse", "HEAD").stdout.strip() == head
        assert (fresh / "README.md").read_text() == "handoff README\n"
