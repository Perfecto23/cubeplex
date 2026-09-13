from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cubeloop.providers.faux import FauxProvider, faux_assistant_message, faux_tool_call

from cubeplex_native.worker import NativeWorker
from cubeplex_native.workspace import Workspace


class FakeControlPlane:
    def __init__(self) -> None:
        self.dispatch_id = "11111111-1111-4111-8111-111111111111"
        self.calls: list[tuple[str, Any]] = []
        self.control_error = False
        self.saved_answer: Any = None

    async def claim(self) -> dict[str, Any]:
        return {
            "owner": True,
            "dispatch_id": self.dispatch_id,
            "run_id": "run-1",
            "conversation_id": "conversation-1",
            "session_id": "session-1",
            "operation": "prompt",
            "content": "say done",
            "system_prompt": "Use native tools.",
            "model": {"id": "fixture-model", "max_output_tokens": 64},
            "reasoning": {"mode": "auto", "effort": "low", "summary": "auto"},
        }

    async def workspace_get(self) -> dict[str, Any]:
        return {"files": []}

    async def workspace_put(self, files: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(("workspace", files))
        return {"file_count": len(files)}

    async def present(self, file: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("present", file))
        return {"file_id": "presented"}

    async def events(self, seq: int, event: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("event", (seq, event["type"])))
        return {"seq": seq}

    async def control(self) -> dict[str, Any]:
        if self.control_error:
            raise RuntimeError("control_unavailable")
        return {"status": "running", "stop_requested": False}

    async def checkpoint(self, op: str, args: dict[str, Any] | None = None) -> Any:
        self.calls.append((op, args))
        if op == "load":
            return None
        return {}

    async def save_pending_request(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def load_pending_request(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def save_hitl_answer(
        self, thread_id: str, question_id: str, answer: Any, **kwargs: Any
    ) -> None:
        self.saved_answer = answer

    async def load_hitl_answer(self, *args: Any, **kwargs: Any) -> Any:
        return self.saved_answer

    async def clear_hitl_answers(self, *args: Any, **kwargs: Any) -> None:
        self.saved_answer = None

    async def model_response(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("FauxProvider must prevent model HTTP")

    async def finish(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("finish", kwargs))
        return {}


@pytest.mark.asyncio
async def test_worker_runs_real_agent_loop_and_remote_checkpointer(tmp_path: Path) -> None:
    client = FakeControlPlane()
    provider = FauxProvider(provider_id="fixture")
    provider.set_responses([faux_assistant_message("done")])
    worker = NativeWorker(
        client,  # type: ignore[arg-type]
        workspace=Workspace(tmp_path),
        model_factory=lambda _client, _config: provider,
    )

    result = await worker.run()

    assert result["status"] == "completed"
    finish = next(value for name, value in client.calls if name == "finish")
    assert finish["status"] == "completed"
    assert any(name == "claim_run" for name, _ in client.calls)
    assert any(name == "append" for name, _ in client.calls)
    assert any(name == "mark_run_complete" for name, _ in client.calls)
    assert any(name == "event" and value[1] == "agent_end" for name, value in client.calls)


@pytest.mark.asyncio
async def test_worker_does_not_execute_when_claim_is_not_owner(tmp_path: Path) -> None:
    client = FakeControlPlane()
    client.claim = lambda: _not_owner()  # type: ignore[method-assign]
    worker = NativeWorker(
        client,
        workspace=Workspace(tmp_path),
    )  # type: ignore[arg-type]

    result = await worker.run()

    assert result == {"status": "not_owner", "dispatch_id": client.dispatch_id}
    assert not client.calls


async def _not_owner() -> dict[str, Any]:
    return {"owner": False}


@pytest.mark.asyncio
async def test_present_file_result_is_wrapped_for_stream_converter(tmp_path: Path) -> None:
    client = FakeControlPlane()
    worker = NativeWorker(client, workspace=Workspace(tmp_path))  # type: ignore[arg-type]
    (tmp_path / "report.txt").write_text("report")

    result = await worker._present_file(
        str(tmp_path / "report.txt"), "report.txt", "cmVwb3J0", "hash", "text/plain"
    )

    assert result == {"presented_file": {"file_id": "presented"}}


@pytest.mark.asyncio
async def test_initialization_failure_finishes_errored(tmp_path: Path) -> None:
    client = FakeControlPlane()

    def fail_factory(_client: Any, _config: Any) -> Any:
        raise RuntimeError("model_init_failed")

    worker = NativeWorker(client, workspace=Workspace(tmp_path), model_factory=fail_factory)
    result = await worker.run()

    assert result["status"] == "errored"
    finish = next(value for name, value in client.calls if name == "finish")
    assert finish["status"] == "errored"


@pytest.mark.asyncio
async def test_control_failure_settles_agent_before_finish(tmp_path: Path) -> None:
    client = FakeControlPlane()
    client.control_error = True
    provider = FauxProvider(provider_id="fixture")
    provider.set_responses([faux_assistant_message("done")])
    worker = NativeWorker(
        client,
        workspace=Workspace(tmp_path),
        model_factory=lambda _client, _config: provider,
    )

    result = await worker.run()

    assert result["status"] == "errored"
    finish_index = next(index for index, (name, _) in enumerate(client.calls) if name == "finish")
    assert all(
        name not in {"append", "mark_run_complete"} for name, _ in client.calls[finish_index + 1 :]
    )


class HitlControlPlane(FakeControlPlane):
    def __init__(self) -> None:
        super().__init__()
        self.operation = "prompt"
        self.messages: list[dict[str, Any]] = []
        self.pending: dict[str, Any] | None = None
        self.finished: list[dict[str, Any]] = []

    async def claim(self) -> dict[str, Any]:
        value = await super().claim()
        value["operation"] = self.operation
        if self.operation == "respond":
            value.update(
                {
                    "question_id": str((self.pending or {}).get("question_id") or ""),
                    "answer": {"choice": "yes"},
                }
            )
        return value

    async def checkpoint(self, op: str, args: dict[str, Any] | None = None) -> Any:
        args = args or {}
        self.calls.append((op, args))
        if op == "load":
            return {"messages": self.messages, "extra": {}}
        if op == "append":
            self.messages.extend(args["messages"])
        elif op == "save_pending_request":
            self.pending = args["request"]
        elif op == "load_pending":
            if self.pending is None:
                return None
            return {"request": self.pending, "run_id": "run-1"}
        elif op == "load_pending_request":
            return self.pending
        elif op == "save_hitl_answer":
            self.saved_answer = args.get("answer")
        elif op == "load_hitl_answer":
            return self.saved_answer
        elif op == "clear_hitl_answers":
            self.saved_answer = None
        return {}

    async def finish(self, **kwargs: Any) -> dict[str, Any]:
        self.finished.append(kwargs)
        return await super().finish(**kwargs)


@pytest.mark.asyncio
async def test_hitl_pause_then_new_worker_responds_same_run(tmp_path: Path) -> None:
    client = HitlControlPlane()
    first_provider = FauxProvider(provider_id="first")
    first_provider.set_responses(
        [
            faux_assistant_message(
                faux_tool_call(
                    "ask_user",
                    {"questions": [{"key": "choice", "prompt": "Continue?"}]},
                )
            )
        ]
    )
    first = NativeWorker(
        client, workspace=Workspace(tmp_path), model_factory=lambda _client, _config: first_provider
    )

    paused = await first.run()

    assert paused["status"] == "paused_hitl"
    assert client.pending is not None
    assert client.finished[-1]["status"] == "paused_hitl"
    client.operation = "respond"
    second_provider = FauxProvider(provider_id="second")
    second_provider.set_responses([faux_assistant_message("continued")])
    second = NativeWorker(
        client,
        workspace=Workspace(tmp_path),
        model_factory=lambda _client, _config: second_provider,
    )

    completed = await second.run()

    assert completed["status"] == "completed"
    assert client.finished[-1]["status"] == "completed"
    assert any(name == "mark_run_complete" for name, _ in client.calls)
