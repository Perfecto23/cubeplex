"""MicroVM-local Git/CubeLoop worker.

This module deliberately has no CubePlex imports.  The VM owns only the
workspace, normal Git, shell processes and a task-scoped broker capability.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import BaseModel, Field

from .client import BrokerClient
from .metrics import Metrics
from .model import BrokerOpenAIResponsesProvider, ModelConfig
from .probe import run_probe
from .snapshot import capture_snapshot, restore_snapshot


class WorkerError(RuntimeError):
    pass


class CloneRequest(BaseModel):
    remote: str = ""


class ShellRequest(BaseModel):
    command: str = Field(min_length=1, max_length=16_384)
    timeout_seconds: int = Field(default=120, ge=1, le=900)


class PullRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=16_000)


def _result(text: str, *, error: bool = False, terminate: bool = False) -> Any:
    from cubeloop import AgentToolResult, TextContent

    return AgentToolResult(
        content=[TextContent(text=text[:32_000])],
        is_error=error,
        terminate=terminate,
    )


class GitWorkspace:
    def __init__(
        self, root: Path, *, config: Mapping[str, Any], broker: BrokerClient, metrics: Metrics
    ):
        self.root = root
        self.config = config
        self.broker = broker
        self.metrics = metrics
        self._bin = root.parent / ".agentcore-bin"
        self._task_config = root / ".git-task-config.json"

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self._bin}:{env.get('PATH', '')}"
        env["CUBEPLEX_GIT_TASK_CONFIG"] = str(self._task_config)
        return env

    def _object_bytes(self) -> int:
        objects = self.repo / ".git" / "objects"
        return sum(path.stat().st_size for path in objects.rglob("*") if path.is_file())

    @staticmethod
    def _trace_received_bytes(stderr: bytes) -> int | None:
        values = [
            int(value.replace(",", ""))
            for value in re.findall(
                r"(?:Recv|received).*?([0-9][0-9,]*) bytes", stderr.decode(errors="ignore"), re.I
            )
        ]
        return sum(values) if values else None

    def install_remote_helper(self) -> None:
        self._bin.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self._task_config.write_text(
            json.dumps(
                {
                    "broker_function_arn": self.config["broker_function_arn"],
                    "region": self.config["region"],
                    "task_id": self.config["task_id"],
                    "stage": self.config["stage"],
                    "capability": self.config["capability"],
                    "repo": self.config["repo"],
                    "remote_url": self.config["remote_url"],
                    "base_sha": self.config["base_sha"],
                    "branch": self.config["branch"],
                },
                separators=(",", ":"),
            )
        )
        self._task_config.chmod(0o600)
        helper = self._bin / "git-remote-broker"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            "from cubeplex_git_slice.git_remote import main\n"
            "raise SystemExit(main())\n"
        )
        helper.chmod(0o700)

    async def clone_repository(self) -> dict[str, Any]:
        self.install_remote_helper()
        branch = str(self.config["branch"])
        remote = str(self.config["remote_url"])
        base = str(self.config["base_sha"])
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.repo / ".git").exists():
            started = time.perf_counter()
            current_remote = await self._command(
                "git", "-C", str(self.repo), "remote", "get-url", "origin"
            )
            if current_remote.strip() != remote:
                raise WorkerError("repository_remote_mismatch")
            fetch = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self.repo),
                "fetch",
                "--no-tags",
                "origin",
                branch,
                env={**self._env(), "GIT_TRACE_CURL": "1", "GIT_TRACE_CURL_NO_DATA": "1"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, fetch_stderr = await fetch.communicate()
            if fetch.returncode:
                raise WorkerError("reuse_fetch_failed")
            self.metrics.clone.reuse_ms = (time.perf_counter() - started) * 1000
            self.metrics.clone.reuse_received_bytes = self._trace_received_bytes(fetch_stderr)
            self.metrics.clone.reuse_object_bytes = self._object_bytes()
            return {"mode": "reuse", "ms": self.metrics.clone.reuse_ms}
        started = time.perf_counter()
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--origin",
            "origin",
            remote,
            str(self.repo),
            env={**self._env(), "GIT_TRACE_CURL": "1", "GIT_TRACE_CURL_NO_DATA": "1"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode:
            raise WorkerError(f"clone_failed:{stderr.decode(errors='replace')[:512]}")
        await self._run_checked("git", "-C", str(self.repo), "checkout", "-B", branch, base)
        await self._run_checked(
            "git", "-C", str(self.repo), "config", "user.name", "CubePlex Agent"
        )
        await self._run_checked(
            "git",
            "-C",
            str(self.repo),
            "config",
            "user.email",
            "agentcore-git-slice@invalid.example",
        )
        await self._run_checked(
            "git",
            "-C",
            str(self.repo),
            "remote",
            "set-url",
            "--push",
            "origin",
            f"broker::{self.config['task_id']}",
        )
        self.metrics.clone.fresh_clone_ms = (time.perf_counter() - started) * 1000
        self.metrics.clone.fresh_received_bytes = self._trace_received_bytes(stderr)
        self.metrics.clone.fresh_object_bytes = self._object_bytes()
        return {"mode": "fresh", "ms": self.metrics.clone.fresh_clone_ms}

    async def _command(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.repo if self.repo.exists() else None,
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode:
            raise WorkerError(err.decode(errors="replace")[:512])
        return out.decode(errors="replace")

    async def _run_checked(self, *args: str) -> str:
        return await self._command(*args)

    async def shell(self, request: ShellRequest) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            request.command,
            cwd=self.repo,
            env=self._env(),
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output = bytearray()
        capped = False

        async def read_output() -> None:
            nonlocal capped
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    return
                remaining = 32_000 - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0) and not capped:
                    capped = True
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

        reader = asyncio.create_task(read_output())
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=request.timeout_seconds)
            await reader
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            await reader
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=2))
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.shield(proc.wait())
            await asyncio.shield(reader)
            raise
        finally:
            if not reader.done():
                reader.cancel()
                try:
                    await reader
                except asyncio.CancelledError:
                    pass
        return {
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "output_capped": capped,
            "stdout": output.decode(errors="replace"),
        }


class GitSliceWorker:
    def __init__(
        self,
        *,
        broker: BrokerClient,
        workspace: Path,
        boot_id: str,
        model_factory: Callable[[BrokerClient, ModelConfig], Any] = BrokerOpenAIResponsesProvider,
    ) -> None:
        self.broker = broker
        self.workspace = workspace
        self.boot_id = boot_id
        self.model_factory = model_factory
        self.metrics = Metrics(boot_id=boot_id)
        self.manifest: dict[str, Any] | None = None
        self.git: GitWorkspace | None = None

    async def _load_manifest(self) -> dict[str, Any]:
        manifest = await self.broker.manifest()
        required = {"repo", "remote_url", "base_sha", "branch", "allowed_paths", "artifact_paths"}
        if not required.issubset(manifest):
            raise WorkerError("manifest_incomplete")
        self.manifest = manifest
        return manifest

    async def clone_tool(
        self,
        tool_call_id: str,
        request: CloneRequest,
        *,
        signal: asyncio.Event | None = None,
        on_update: Callable | None = None,
    ) -> Any:
        del tool_call_id, signal, on_update
        if self.git is None:
            raise WorkerError("workspace_not_ready")
        if request.remote and request.remote != self.git.config["remote_url"]:
            return _result("remote is fixed by the broker manifest", error=True)
        return _result(json.dumps(await self.git.clone_repository(), sort_keys=True))

    async def shell_tool(
        self,
        tool_call_id: str,
        request: ShellRequest,
        *,
        signal: asyncio.Event | None = None,
        on_update: Callable | None = None,
    ) -> Any:
        del tool_call_id, signal, on_update
        if self.git is None:
            raise WorkerError("workspace_not_ready")
        self.metrics.tool_calls += 1
        return _result(json.dumps(await self.git.shell(request), sort_keys=True))

    async def pull_request_tool(
        self,
        tool_call_id: str,
        request: PullRequest,
        *,
        signal: asyncio.Event | None = None,
        on_update: Callable | None = None,
    ) -> Any:
        del tool_call_id, signal, on_update
        if self.git is None or self.manifest is None:
            raise WorkerError("workspace_not_ready")
        head = await self.git._run_checked("git", "rev-parse", "HEAD")
        result = await self.broker.create_pull_request(
            {
                "repo": self.manifest["repo"],
                "branch": self.manifest["branch"],
                "commit": head.strip(),
                "title": request.title,
                "body": request.body,
            }
        )
        return _result(json.dumps(result, sort_keys=True))

    def _tools(self) -> list[Any]:
        from cubeloop import AgentTool

        return [
            AgentTool(
                "clone_repository",
                "Clone or reuse the fixed repository.",
                CloneRequest,
                self.clone_tool,
            ),
            AgentTool(
                "shell",
                "Run a bounded shell command inside the repository.",
                ShellRequest,
                self.shell_tool,
                execution_mode="sequential",
            ),
            AgentTool(
                "create_pull_request",
                "Create or read back the fixed repository pull request.",
                PullRequest,
                self.pull_request_tool,
                execution_mode="sequential",
            ),
        ]

    async def run(self, stage: str) -> dict[str, Any]:
        status = await self.broker.status()
        initial_model_calls = int(status.get("model_calls", 0))
        completed = status.get("completed_stages") or {}
        if isinstance(completed, dict) and stage in completed:
            return {"status": "already_completed", "stage": stage, "result": completed[stage]}
        manifest = await self._load_manifest()
        probe = await run_probe(self.broker, canary_secret_arn=manifest.get("canary_secret_arn"))
        if probe.canary != "iam_denied":
            raise WorkerError("canary_probe_failed")
        if probe.platform_master_surface != "aws_task_role_or_capability_only":
            raise WorkerError("platform_credential_probe_failed")
        worker_config = {
            **manifest,
            "broker_function_arn": self.broker.config.function_arn,
            "region": self.broker.config.region,
            "task_id": self.broker.config.task_id,
            "stage": self.broker.config.stage,
            "capability": self.broker.config.capability,
        }
        self.git = GitWorkspace(
            self.workspace,
            config=worker_config,
            broker=self.broker,
            metrics=self.metrics,
        )
        seeded_messages: list[Any] | None = None
        if stage == "resume":
            snapshot = await self.broker.checkpoint_get()
            if snapshot:
                await self.git.clone_repository()
                started = time.perf_counter()
                restore_snapshot(
                    self.git.repo,
                    snapshot,
                    branch=str(manifest["branch"]),
                    base_sha=str(manifest["base_sha"]),
                )
                self.metrics.clone.restore_ms = (time.perf_counter() - started) * 1000
                from pydantic import TypeAdapter
                from cubeloop.providers.base import Message

                seeded_messages = [
                    TypeAdapter(Message).validate_python(item)
                    for item in snapshot.get("messages", [])
                ]
                await self.git.clone_repository()
        provider = self.model_factory(
            self.broker,
            ModelConfig(
                model=str(manifest["model"]),
                max_output_tokens=int(manifest.get("max_model_output_tokens", 2048)),
            ),
        )
        model = provider.model(
            str(manifest["model"]),
            reasoning=True,
            max_tokens=int(manifest.get("max_model_output_tokens", 2048)),
        )
        from cubeloop import Agent, ReasoningControl

        agent: Any = Agent(
            model=model,
            tools=self._tools(),
            messages=seeded_messages,
            reasoning=ReasoningControl(mode="auto", effort="low", summary="auto"),
            system_prompt=(
                "Work only in the fixed repository. Use clone_repository first, then shell. "
                "Only intervals.py may change in the commit. Fix the failing test, run the "
                "targeted tests, commit, push through the broker remote helper, and create "
                "the pull request. Before completion, leave README.md with a concise handoff "
                "note and create an untracked continuation.md for the next VM. "
                "Do not print credentials or capability strings."
            ),
        )
        prompt = (
            "Resume the existing handoff without redoing the fix. Verify HEAD, run the focused "
            "tests, and idempotently confirm push and pull request publication."
            if stage == "resume"
            else (
                "Clone the repository and run python -m unittest -v BEFORE editing to record "
                "the failing baseline. Inspect and fix intervals.py, run the same tests again, "
                "commit only intervals.py, push the current branch to origin, and create the PR. "
                "Then leave the README.md handoff patch and untracked continuation.md."
            )
        )
        try:
            await agent.prompt(prompt)
        finally:
            private_client = getattr(provider, "_client", None)
            close = getattr(private_client, "close", None)
            if close is not None:
                await close()
        if agent.state.last_outcome != "complete":
            raise WorkerError(f"agent_outcome_{agent.state.last_outcome or 'unknown'}")
        status_text = await self.git._run_checked(
            "git", "status", "--porcelain", "--untracked-files=all"
        )
        if stage == "work":
            if "README.md" not in status_text:
                raise WorkerError("handoff_readme_change_missing")
            if not (self.git.repo / "continuation.md").exists():
                raise WorkerError("handoff_continuation_missing")
        result = await self.broker.status()
        commit = str(result.get("commit") or "")
        head = (await self.git._run_checked("git", "rev-parse", "HEAD")).strip()
        pr = result.get("pr")
        if commit != head or not isinstance(pr, dict) or not pr.get("number") or not pr.get("url"):
            raise WorkerError("publication_incomplete")
        if stage == "work":
            # Measure reuse only after the Agent's branch exists on GitHub.
            await self.git.clone_repository()
        self.metrics.model_calls = max(
            self.metrics.model_calls,
            int(result.get("model_calls", initial_model_calls)) - initial_model_calls,
        )
        snapshot = capture_snapshot(
            self.git.repo,
            stage=stage,
            boot_id=self.boot_id,
            base_sha=str(manifest["base_sha"]),
            branch=str(manifest["branch"]),
            native_messages=[m.model_dump(mode="json") for m in agent.state.messages],
            metrics={**self.metrics.to_dict(), "probe": probe.to_dict()},
            result=result,
            artifact_paths=set(manifest["artifact_paths"]),
            max_snapshot_bytes=int(manifest.get("max_snapshot_bytes", 4_194_304)),
        )
        await self.broker.checkpoint_put(snapshot)
        return {
            "status": "complete",
            "stage": stage,
            "result": result,
            "metrics": self.metrics.to_dict(),
        }
