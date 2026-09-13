"""CubeLoop worker owned by one control-plane dispatch."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Literal, cast

from cubeloop import Agent, ReasoningControl
from cubeloop.agent.types import AgentEvent
from cubeloop.hitl import CheckpointedChannel, ask_user_tool

from .checkpointer import RemoteCheckpointer
from .client import ControlPlaneError, NativeControlPlaneClient
from .model import ModelConfig, NativeOpenAIResponsesProvider
from .tools import NativeTools
from .workspace import Workspace


class NativeWorkerError(RuntimeError):
    """A native dispatch cannot be completed safely."""


class NativeWorker:
    def __init__(
        self,
        client: NativeControlPlaneClient,
        *,
        workspace: Workspace | None = None,
        model_factory: Callable[
            [NativeControlPlaneClient, ModelConfig], Any
        ] = NativeOpenAIResponsesProvider,
    ) -> None:
        self.client = client
        self.workspace = workspace or Workspace("/workspace")
        self.model_factory = model_factory
        self._event_seq = 0
        self._hitl_event = asyncio.Event()
        self._operation = ""
        self._run_id = ""
        self._skip_initial_prompt_user = False

    async def _on_event(self, event: AgentEvent, signal: asyncio.Event | None = None) -> None:
        del signal
        if (
            self._operation == "prompt"
            and self._skip_initial_prompt_user
            and event.type == "message_end"
            and getattr(event.message, "role", None) == "user"
            and getattr(event.message, "run_id", None) == self._run_id
        ):
            self._skip_initial_prompt_user = False
            return
        payload = event.model_dump(mode="json")
        await self.client.events(self._event_seq + 1, payload)
        self._event_seq += 1
        if payload.get("type") in {"hitl_request", "agent_suspended"}:
            self._hitl_event.set()

    async def _restore_workspace(self) -> None:
        response = await self.client.workspace_get()
        files = response.get("files") or []
        if not files:
            return
        if not isinstance(files, list):
            raise NativeWorkerError("workspace_files_invalid")
        self.workspace.restore_snapshot({"schema_version": 1, "files": files})

    async def _save_workspace(self) -> None:
        snapshot = self.workspace.snapshot()
        await self.client.workspace_put(snapshot["files"])

    async def _present_file(
        self,
        path: str,
        name: str,
        content_b64: str,
        sha256: str,
        mime_type: str | None,
    ) -> Any:
        try:
            relative = str(Path(path).resolve().relative_to(self.workspace.root.resolve()))
        except ValueError as exc:
            raise NativeWorkerError("present_path_outside_workspace") from exc
        result = await self.client.present(
            {
                "path": relative,
                "name": name,
                "content_b64": content_b64,
                "sha256": sha256,
                "mime_type": mime_type,
            }
        )
        return {"presented_file": result}

    async def _control_loop(self, agent: Agent) -> str | None:
        while True:
            control = await self.client.control()
            if control.get("stop_requested") is True:
                agent.abort()
                return "cancelled"
            if self._hitl_event.is_set() and agent.in_flight_hitl_request is not None:
                self._hitl_event.clear()
                await agent.detach()
                return "paused_hitl"
            try:
                await asyncio.wait_for(self._hitl_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _reasoning(value: Any) -> ReasoningControl:
        mode = "auto"
        effort = "low"
        summary = "auto"
        if isinstance(value, dict):
            mode = str(value.get("mode", mode))
            effort = str(value.get("effort", effort))
            summary = str(value.get("summary", summary))
        return ReasoningControl(
            mode=cast(Literal["off", "auto", "on"], mode),
            effort=cast(Literal["minimal", "low", "medium", "high", "max"], effort),
            summary=cast(Literal["none", "auto", "detailed", "summarized"], summary),
        )

    @staticmethod
    async def _settle_run(agent: Agent[Any] | None, task: asyncio.Task[Any] | None) -> None:
        if agent is not None:
            agent.abort()
        if task is None or task.done():
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except BaseException:
            await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> dict[str, Any]:
        claim = await self.client.claim()
        if claim.get("owner") is not True:
            return {"status": "not_owner", "dispatch_id": self.client.dispatch_id}
        run_id = str(claim.get("run_id") or "")
        conversation_id = str(claim.get("conversation_id") or "")
        operation = str(claim.get("operation") or "")
        provider: Any | None = None
        agent: Agent[Any] | None = None

        def unsubscribe() -> Any:
            return None

        run_task: asyncio.Task[Any] | None = None
        control_task: asyncio.Task[str | None] | None = None
        status = "errored"
        error_code: str | None = None
        try:
            if not run_id or not conversation_id or operation not in {"prompt", "respond"}:
                raise NativeWorkerError("claim_shape_invalid")
            self._operation = operation
            self._run_id = run_id
            self._skip_initial_prompt_user = operation == "prompt"

            await self._restore_workspace()
            checkpointer = RemoteCheckpointer(self.client, conversation_id)
            channel = CheckpointedChannel(
                checkpointer=checkpointer,
                thread_id=conversation_id,
                run_id=run_id,
            )
            native_tools = NativeTools(self.workspace, present_callback=self._present_file)
            tools = [*native_tools.as_cubeloop_tools(), ask_user_tool(channel)]
            model_config = ModelConfig(
                model=str((claim.get("model") or {}).get("id") or ""),
                max_output_tokens=int((claim.get("model") or {}).get("max_output_tokens", 2048)),
            )
            if not model_config.model:
                raise NativeWorkerError("model_config_invalid")
            provider = self.model_factory(self.client, model_config)
            agent = Agent(
                model=provider.model(
                    model_config.model,
                    reasoning=True,
                    max_tokens=model_config.max_output_tokens,
                ),
                system_prompt=str(claim.get("system_prompt") or ""),
                tools=tools,
                reasoning=self._reasoning(claim.get("reasoning")),
                checkpointer=checkpointer,
                thread_id=conversation_id,
                channel=channel,
                tool_execution="sequential",
            )
            unsubscribe = agent.subscribe(self._on_event)
            run_task = asyncio.create_task(
                agent.prompt(str(claim.get("content") or ""), run_id=run_id)
                if operation == "prompt"
                else agent.respond(
                    question_id=str(claim.get("question_id") or ""),
                    answer=claim.get("answer"),
                )
            )
            control_task = asyncio.create_task(self._control_loop(agent))
            done, _ = await asyncio.wait(
                {run_task, control_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if control_task in done:
                try:
                    control_outcome = control_task.result()
                except ControlPlaneError as exc:
                    status = "errored"
                    error_code = exc.code
                    await self._settle_run(agent, run_task)
                except Exception as exc:
                    status = "errored"
                    error_code = type(exc).__name__.lower()
                    await self._settle_run(agent, run_task)
                else:
                    if control_outcome in {"paused_hitl", "cancelled"}:
                        status = control_outcome
                        await self._settle_run(agent, run_task)
                    else:
                        status = "errored"
                        error_code = "control_outcome_invalid"
                        await self._settle_run(agent, run_task)
            else:
                await run_task
                if agent.state.last_outcome in {"suspended", "paused_hitl"}:
                    status = "paused_hitl"
                elif agent.state.last_outcome != "complete":
                    status = "errored"
                    error_code = "agent_not_completed"
                else:
                    status = "completed"
        except asyncio.CancelledError:
            status = "cancelled"
            await self._settle_run(agent, run_task)
        except ControlPlaneError as exc:
            status = "errored"
            error_code = exc.code
            await self._settle_run(agent, run_task)
        except Exception as exc:
            status = "errored"
            error_code = type(exc).__name__.lower()
            await self._settle_run(agent, run_task)
        finally:
            await self._settle_run(agent, run_task)
            if control_task is not None and not control_task.done():
                control_task.cancel()
                await asyncio.gather(control_task, return_exceptions=True)
            unsubscribe()
            try:
                await self._save_workspace()
            except Exception as exc:
                if status in {"completed", "paused_hitl"}:
                    status = "errored"
                    error_code = type(exc).__name__.lower()
            try:
                await self.client.finish(
                    request_id=str(__import__("uuid").uuid4()),
                    status=status,
                    error_code=error_code,
                )
            finally:
                if provider is not None:
                    close = getattr(provider, "aclose", None)
                    if close is not None:
                        await close()
        return {"status": status, "dispatch_id": self.client.dispatch_id, "error_code": error_code}
