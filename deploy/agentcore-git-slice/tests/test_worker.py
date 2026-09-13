from __future__ import annotations

import subprocess

import pytest

from cubeloop import Agent
from cubeloop.providers.faux import FauxProvider, faux_assistant_message, faux_tool_call

from cubeplex_git_slice.metrics import Metrics
from cubeplex_git_slice.worker import GitSliceWorker, GitWorkspace, ShellRequest


class FakeBroker:
    pass


@pytest.mark.asyncio
async def test_shell_timeout_terminates_process_group(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = GitWorkspace(
        tmp_path,
        config={"remote_url": "file:///fixture", "branch": "main", "base_sha": "0"},
        broker=FakeBroker(),
        metrics=Metrics("boot"),
    )
    result = await workspace.shell(
        ShellRequest(command="trap '' TERM; sleep 30", timeout_seconds=1)
    )
    assert result["timed_out"] is True
    assert result["exit_code"] is not None


@pytest.mark.asyncio
async def test_local_clone_reuses_same_environment(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
    (source / "intervals.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=source, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()
    workspace = GitWorkspace(
        tmp_path / "workspace",
        config={
            "remote_url": str(source),
            "branch": "main",
            "base_sha": base,
            "task_id": "task",
            "repo": "fixture/repo",
            "broker_function_arn": "arn:fixture",
            "region": "us-west-2",
            "stage": "work",
            "capability": "cap",
        },
        broker=FakeBroker(),
        metrics=Metrics("boot"),
    )
    first = await workspace.clone_repository()
    second = await workspace.clone_repository()
    assert first["mode"] == "fresh"
    assert second["mode"] == "reuse"
    assert workspace.metrics.clone.reuse_ms is not None


@pytest.mark.asyncio
async def test_real_cubeloop_agent_executes_clone_and_shell_tools(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
    (source / "intervals.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=source, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()

    class Broker:
        config = type(
            "Config",
            (),
            {
                "function_arn": "arn",
                "region": "us-west-2",
                "task_id": "task",
                "stage": "work",
                "capability": "cap",
            },
        )()

    worker = GitSliceWorker(broker=Broker(), workspace=tmp_path / "workspace", boot_id="boot")
    worker.manifest = {
        "remote_url": str(source),
        "repo": "fixture/repo",
        "base_sha": base,
        "branch": "main",
        "broker_function_arn": "arn",
        "region": "us-west-2",
        "task_id": "task",
        "stage": "work",
        "capability": "cap",
    }
    worker.git = GitWorkspace(
        tmp_path / "workspace",
        config=worker.manifest,
        broker=Broker(),
        metrics=worker.metrics,
    )
    provider = FauxProvider(provider_id="test")
    provider.set_responses(
        [
            faux_assistant_message(faux_tool_call("clone_repository", {})),
            faux_assistant_message(faux_tool_call("shell", {"command": "git status --short"})),
            faux_assistant_message("complete"),
        ]
    )
    agent = Agent(model=provider.model("fixture-model"), tools=worker._tools())
    await agent.prompt("Use the clone and shell tools.")
    assert agent.state.last_outcome == "complete"
    assert (worker.git.repo / "intervals.py").exists()
    assert provider.call_count == 3
