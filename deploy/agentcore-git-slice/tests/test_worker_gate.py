from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import cubeplex_git_slice.worker as worker_module
from cubeplex_git_slice.client import BrokerConfig
from cubeplex_git_slice.worker import GitSliceWorker, WorkerError


@pytest.mark.asyncio
async def test_resume_platform_probe_failure_precedes_clone_checkpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    broker = MagicMock()
    broker.config = BrokerConfig("arn", "us-west-2", "task", "resume", "cap")
    broker.status = AsyncMock(return_value={})
    worker = GitSliceWorker(broker=broker, workspace=tmp_path, boot_id="boot")
    manifest = {
        "repo": "fixture/repo",
        "remote_url": "https://example.invalid/repo.git",
        "base_sha": "0" * 40,
        "branch": "agentcore/fix-inclusive-total",
        "allowed_paths": ["intervals.py"],
        "artifact_paths": ["README.md", "continuation.md"],
        "model": "fixture-model",
        "canary_secret_arn": "arn:canary",
    }
    monkeypatch.setattr(worker, "_load_manifest", AsyncMock(return_value=manifest))
    monkeypatch.setattr(
        worker_module,
        "run_probe",
        AsyncMock(
            return_value=SimpleNamespace(platform_master_surface="fail_closed", canary="iam_denied")
        ),
    )
    clone = AsyncMock()
    monkeypatch.setattr(worker_module.GitWorkspace, "clone_repository", clone)
    broker.checkpoint_get = AsyncMock(side_effect=AssertionError("checkpoint must not run"))
    model_factory = MagicMock(side_effect=AssertionError("model must not run"))
    worker.model_factory = model_factory

    with pytest.raises(WorkerError, match="platform_credential_probe_failed"):
        await worker.run("resume")
    clone.assert_not_awaited()
    broker.checkpoint_get.assert_not_awaited()
    model_factory.assert_not_called()
