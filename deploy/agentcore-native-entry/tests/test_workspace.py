from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from pathlib import Path

import pytest

from cubeplex_native.workspace import (
    Workspace,
    WorkspaceError,
    WorkspaceLimitError,
    WorkspaceLimits,
    WorkspacePathError,
    WorkspaceSnapshotError,
)


def test_file_tools_are_workspace_scoped_and_atomic(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    created = workspace.write_file("src/result.txt", "before\n")
    assert created["path"] == "src/result.txt"
    assert workspace.read_file("src/result.txt")["content"] == "before\n"
    with pytest.raises(WorkspaceError, match="file_exists"):
        workspace.write_file("src/result.txt", "clobber\n")

    edited = workspace.edit_file("src/result.txt", "before", "after")
    assert edited["replacements"] == 1
    assert workspace.read_file("src/result.txt")["content"] == "after\n"
    with pytest.raises(WorkspacePathError, match="path_invalid"):
        workspace.read_file("../outside")
    with pytest.raises(WorkspacePathError, match="path_invalid"):
        workspace.read_file("a\\b")


def test_existing_symlink_parent_is_rejected(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (real / "public.txt").write_text("public")
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    with pytest.raises(WorkspacePathError, match="path_parent_is_symlink"):
        workspace.read_file("alias/public.txt")


def test_read_file_uses_the_requested_byte_bound(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "large.txt").write_bytes(b"x" * 64)
    with pytest.raises(WorkspaceLimitError, match="file_too_large"):
        workspace.read_file("large.txt", max_bytes=16)


def test_snapshot_is_bounded_and_excludes_runtime_credentials(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "public.txt").write_text("public")
    (tmp_path / ".env").write_text("TOKEN=must-not-snapshot")
    (tmp_path / ".agentcore-runtime.json").write_text("runtime")
    (tmp_path / "credentials.json").write_text("secret")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".git" / "config").write_text("[remote]\nurl=https://token@example\n")

    snapshot = workspace.snapshot()
    paths = [entry["path"] for entry in snapshot["files"]]
    assert paths == ["public.txt"]
    assert all("content_b64" in entry and "sha256" in entry for entry in snapshot["files"])


def test_snapshot_rejects_links_and_hardlinks(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "real.txt").write_text("data")
    (tmp_path / "linked.txt").symlink_to(tmp_path / "real.txt")
    with pytest.raises(WorkspaceSnapshotError, match="symlink_not_allowed"):
        workspace.snapshot()

    (tmp_path / "linked.txt").unlink()
    os.link(tmp_path / "real.txt", tmp_path / "hardlinked.txt")
    with pytest.raises(WorkspaceSnapshotError, match="hardlink_not_allowed"):
        workspace.snapshot()


def test_restore_validates_the_whole_package_before_landing(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.write_file("result.txt", "original")
    digest = hashlib.sha256(b"new").hexdigest()
    snapshot = {
        "schema_version": 1,
        "files": [
            {
                "path": "result.txt",
                "content_b64": base64.b64encode(b"new").decode(),
                "sha256": digest,
            },
            {
                "path": "second.txt",
                "content_b64": base64.b64encode(b"bad").decode(),
                "sha256": "0" * 64,
            },
        ],
        "total_bytes": 6,
    }
    with pytest.raises(WorkspaceSnapshotError, match="snapshot_hash_mismatch"):
        workspace.restore_snapshot(snapshot)
    assert workspace.read_file("result.txt")["content"] == "original"
    assert not (tmp_path / "second.txt").exists()


def test_restore_does_not_touch_runtime_configuration(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    runtime = tmp_path / ".agentcore-runtime.json"
    runtime.write_text("live")
    snapshot = {
        "schema_version": 1,
        "files": [
            {
                "path": ".agentcore-runtime.json",
                "content_b64": base64.b64encode(b"replace").decode(),
                "sha256": hashlib.sha256(b"replace").hexdigest(),
            }
        ],
        "total_bytes": 7,
    }
    with pytest.raises(WorkspaceSnapshotError, match="snapshot_protected_path"):
        workspace.restore_snapshot(snapshot)
    assert runtime.read_text() == "live"


@pytest.mark.parametrize("path", [".git/HEAD", ".hidden", "credentials.json"])
def test_restore_rejects_hidden_git_and_credential_paths(tmp_path: Path, path: str) -> None:
    workspace = Workspace(tmp_path)
    content = b"public"
    snapshot = {
        "schema_version": 1,
        "files": [
            {
                "path": path,
                "content_b64": base64.b64encode(content).decode(),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "total_bytes": len(content),
    }
    with pytest.raises(WorkspaceSnapshotError, match="snapshot_protected_path"):
        workspace.restore_snapshot(snapshot)


@pytest.mark.asyncio
async def test_execute_uses_workspace_cwd_and_caps_output(tmp_path: Path) -> None:
    workspace = Workspace(
        tmp_path,
        limits=WorkspaceLimits(max_output_bytes=256),
    )
    workspace.write_file("cwd.txt", "ok")
    result = await workspace.execute("pwd; cat cwd.txt; printf 'x%.0s' {1..100}")
    assert result["stdout"].startswith(str(tmp_path))
    assert "ok" in result["stdout"]
    assert result["output_capped"] is False
    assert len(result["stdout"].encode()) <= 256

    capped = await Workspace(
        tmp_path,
        limits=WorkspaceLimits(max_output_bytes=32),
    ).execute("printf 'x%.0s' {1..100}")
    assert capped["output_capped"] is True
    assert len(capped["stdout"].encode()) <= 32


@pytest.mark.asyncio
async def test_execute_timeout_kills_the_process_group(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    started = time.monotonic()
    result = await workspace.execute("trap '' TERM; sleep 30", timeout_seconds=0.2)
    assert time.monotonic() - started < 5
    assert result["timed_out"] is True
    assert result["exit_code"] is not None


@pytest.mark.asyncio
async def test_execute_kills_background_child_after_leader_exit(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    started = time.monotonic()
    result = await workspace.execute(
        "(sleep 0.7; touch late.txt) & exit 0",
        timeout_seconds=0.1,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.6
    assert result["timed_out"] is True
    await asyncio.sleep(0.8)
    assert not (tmp_path / "late.txt").exists()


@pytest.mark.asyncio
async def test_execute_cancel_kills_the_process_group(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    cancel = asyncio.Event()
    task = asyncio.create_task(workspace.execute("trap '' TERM; sleep 30", cancel=cancel))
    await asyncio.sleep(0.1)
    cancel.set()
    result = await task
    assert result["cancelled"] is True
    assert result["exit_code"] is not None


@pytest.mark.asyncio
async def test_execute_cancel_closes_inherited_stdout_child(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    cancel = asyncio.Event()
    task = asyncio.create_task(
        workspace.execute("(sleep 0.7; touch late-cancel.txt) & cat", cancel=cancel)
    )
    await asyncio.sleep(0.1)
    cancel.set()
    result = await task
    assert result["cancelled"] is True
    await asyncio.sleep(0.8)
    assert not (tmp_path / "late-cancel.txt").exists()
