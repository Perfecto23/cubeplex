"""OpenSandbox command cancellation contracts against SDK 0.1.12."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cubeplex.sandbox.opensandbox import OpenSandbox, SandboxCancellationUnknown


class _FakeCommands:
    def __init__(self, *, expose_id: bool = True, interrupt_ok: bool = True) -> None:
        self.expose_id = expose_id
        self.interrupt_ok = interrupt_ok
        self.started = asyncio.Event()
        self.interrupt_calls: list[str] = []
        self.running = True

    async def run(self, _command: str, *, opts: object, handlers: object) -> object:
        del opts
        self.started.set()
        if self.expose_id:
            await handlers.on_init(SimpleNamespace(id="cmd-1"))  # type: ignore[attr-defined]
        await asyncio.sleep(60)
        return SimpleNamespace(id="cmd-1" if self.expose_id else None, logs=SimpleNamespace())

    async def interrupt(self, execution_id: str) -> None:
        self.interrupt_calls.append(execution_id)
        if not self.interrupt_ok:
            raise RuntimeError("interrupt unavailable")
        self.running = False

    async def get_command_status(self, _execution_id: str) -> object:
        return SimpleNamespace(running=self.running)


def _sandbox(commands: _FakeCommands) -> OpenSandbox:
    return OpenSandbox(sandbox=SimpleNamespace(commands=commands))


@pytest.mark.asyncio
async def test_timeout_interrupts_remote_command_before_returning_marker() -> None:
    commands = _FakeCommands()
    result = await _sandbox(commands).execute("sleep 60", timeout=0.01)

    assert result.output == "[timeout]"
    assert result.exit_code == -1
    assert commands.interrupt_calls == ["cmd-1"]
    assert commands.running is False


@pytest.mark.asyncio
async def test_cancel_interrupts_remote_command_and_reraises_cancelled() -> None:
    commands = _FakeCommands()
    task = asyncio.create_task(_sandbox(commands).execute("sleep 60"))
    await commands.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert commands.interrupt_calls == ["cmd-1"]
    assert commands.running is False


@pytest.mark.asyncio
async def test_interrupt_failure_is_explicit_stop_unknown() -> None:
    commands = _FakeCommands(interrupt_ok=False)

    with pytest.raises(SandboxCancellationUnknown, match="termination failed"):
        await _sandbox(commands).execute("sleep 60", timeout=0.01)

    assert commands.interrupt_calls == ["cmd-1"]


@pytest.mark.asyncio
async def test_timeout_race_interrupt_error_is_confirmed_by_nonrunning_status() -> None:
    commands = _FakeCommands(interrupt_ok=False)
    commands.running = False

    result = await _sandbox(commands).execute("sleep 60", timeout=0.01)

    assert result.output == "[timeout]"
    assert result.exit_code == -1
    assert commands.interrupt_calls == ["cmd-1"]


@pytest.mark.asyncio
async def test_interrupt_network_error_with_running_status_stays_unknown() -> None:
    commands = _FakeCommands(interrupt_ok=False)

    with pytest.raises(SandboxCancellationUnknown, match="termination failed"):
        await _sandbox(commands).execute("sleep 60", timeout=0.01)

    assert commands.interrupt_calls == ["cmd-1"]


@pytest.mark.asyncio
async def test_timeout_without_command_id_is_explicit_stop_unknown() -> None:
    commands = _FakeCommands(expose_id=False)

    with pytest.raises(SandboxCancellationUnknown, match="command id"):
        await _sandbox(commands).execute("sleep 60", timeout=0.01)

    assert commands.interrupt_calls == []
