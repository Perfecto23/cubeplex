from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cubeloop.providers.faux import FauxProvider, faux_assistant_message, faux_tool_call

import cubeplex_git_slice.worker as worker_module
from cubeplex_git_slice.client import BrokerConfig
from cubeplex_git_slice.metrics import Metrics
from cubeplex_git_slice.worker import GitSliceWorker, GitWorkspace


BRANCH = "agentcore/fix-inclusive-total"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, str, str]:
    seed = tmp_path / "seed"
    remote = tmp_path / "remote.git"
    seed.mkdir()
    _git(tmp_path, "init", "--bare", str(remote))
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.name", "fixture")
    _git(seed, "config", "user.email", "fixture@example.invalid")
    (seed / "intervals.py").write_text("def total(values):\n    return sum(values)\n")
    (seed / "README.md").write_text("# fixture\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "base")
    base = _git(seed, "rev-parse", "HEAD")
    _git(seed, "checkout", "-q", "-b", BRANCH)
    (seed / "intervals.py").write_text(
        "def total(values):\n    return sum(values)\n\n# published fix\n"
    )
    _git(seed, "commit", "-qam", "published fix")
    published = _git(seed, "rev-parse", "HEAD")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "main", BRANCH)
    return remote, base, published


def _config(remote: Path, base: str, published: str) -> dict[str, Any]:
    return {
        "remote_url": str(remote),
        "repo": "fixture/repo",
        "base_sha": base,
        "branch": BRANCH,
        "broker_function_arn": "arn:fixture",
        "region": "us-west-2",
        "task_id": "task",
        "stage": "work",
        "capability": "cap",
        "checkout_sha": published,
    }


@pytest.mark.asyncio
async def test_published_checkout_reclone_keeps_original_sha(tmp_path: Path) -> None:
    remote, base, published = _fixture(tmp_path)
    workspace = GitWorkspace(
        tmp_path / "workspace",
        config=_config(remote, base, published),
        broker=SimpleNamespace(),
        metrics=Metrics("boot"),
    )

    first = await workspace.clone_repository()
    assert first["mode"] == "fresh"
    assert first["checkout"] == "published_commit_only"
    assert _git(workspace.repo, "rev-parse", "HEAD") == published

    second = await workspace.clone_repository()
    assert second["mode"] == "reuse"
    assert _git(workspace.repo, "rev-parse", "HEAD") == published
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}") == published


class PublishedBroker:
    def __init__(self, published: str, manifest: dict[str, Any]) -> None:
        self.config = BrokerConfig("arn:fixture", "us-west-2", "task", "work", "cap")
        self.published = published
        self._manifest = manifest
        self.pr_calls: list[dict[str, Any]] = []
        self.snapshot: dict[str, Any] | None = None
        self.pr: dict[str, Any] | None = None

    async def status(self) -> dict[str, Any]:
        return {
            "commit": self.published,
            "pr": self.pr,
            "model_calls": 11,
            "snapshot_sha256": None,
            "completed_stages": {},
        }

    async def manifest(self) -> dict[str, Any]:
        return self._manifest

    async def create_pull_request(self, data: dict[str, Any]) -> dict[str, Any]:
        self.pr_calls.append(data)
        self.pr = {
            "number": 2,
            "url": "https://github.com/fixture/pr/2",
        }
        return {**self.pr, "status": "already_exists"}

    async def checkpoint_put(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.snapshot = snapshot
        return {"snapshot_sha256": "snapshot"}


@pytest.mark.asyncio
async def test_published_continuation_verifies_without_new_fix_or_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, base, published = _fixture(tmp_path)
    manifest = {
        "repo": "fixture/repo",
        "remote_url": str(remote),
        "base_sha": base,
        "branch": BRANCH,
        "allowed_paths": ["intervals.py"],
        "artifact_paths": ["README.md", "continuation.md"],
        "model": "fixture-model",
        "canary_secret_arn": "arn:canary",
        "max_model_output_tokens": 128,
        "max_snapshot_bytes": 4_194_304,
    }
    broker = PublishedBroker(published, manifest)
    probe = SimpleNamespace(
        canary="iam_denied",
        platform_master_surface="aws_task_role_or_capability_only",
        to_dict=lambda: {"canary": "iam_denied"},
    )
    monkeypatch.setattr(worker_module, "run_probe", lambda *args, **kwargs: _async_value(probe))

    provider = FauxProvider(provider_id="published-continuation")
    provider.set_responses(
        [
            faux_assistant_message(faux_tool_call("clone_repository", {})),
            faux_assistant_message(faux_tool_call("shell", {"command": "git rev-parse HEAD"})),
            faux_assistant_message(
                faux_tool_call(
                    "shell",
                    {
                        "command": (
                            "printf '%s\\n' 'published handoff' >> README.md && "
                            "printf '%s\\n' 'continue' > continuation.md"
                        )
                    },
                )
            ),
            faux_assistant_message(
                faux_tool_call(
                    "create_pull_request",
                    {"title": "Confirm published handoff", "body": "Verify existing PR."},
                )
            ),
            faux_assistant_message("published continuation complete"),
        ]
    )
    worker = GitSliceWorker(
        broker=broker,
        workspace=tmp_path / "worker",
        boot_id="boot",
        model_factory=lambda _broker, _config: provider,
    )

    result = await worker.run("work")

    assert result["status"] == "complete"
    assert result["result"]["recovery_mode"] == "published_commit_only"
    assert result["metrics"]["recovery_mode"] == "published_commit_only"
    assert _git(worker.git.repo, "rev-parse", "HEAD") == published
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}") == published
    assert broker.pr_calls[0]["commit"] == published
    assert broker.snapshot is not None
    assert broker.snapshot["metrics"]["recovery_mode"] == "published_commit_only"
    assert broker.snapshot["result"]["recovery_mode"] == "published_commit_only"
    assert (worker.git.repo / "continuation.md").exists()
    assert "README.md" in _git(worker.git.repo, "status", "--porcelain")

    prompt = provider.prompt_cache["default"]
    assert "Do not modify intervals.py" in prompt
    assert "create a commit" in prompt
    assert "Inspect and fix intervals.py" not in prompt
    assert "commit only intervals.py" not in prompt


async def _async_value(value: Any) -> Any:
    return value
