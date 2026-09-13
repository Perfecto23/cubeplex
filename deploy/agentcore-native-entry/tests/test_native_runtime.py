from __future__ import annotations

import pytest
from types import SimpleNamespace

from cubeplex_native.runtime import _input, expected_session_id, invoke
from cubeplex_native.worker import NativeWorkerError


def test_runtime_accepts_only_version_two_dispatch_payload() -> None:
    assert _input(
        {
            "version": 2,
            "dispatch_id": "11111111-1111-4111-8111-111111111111",
            "capability": "cap",
        }
    ) == ("11111111-1111-4111-8111-111111111111", "cap")


def test_runtime_session_id_is_derived_only_from_dispatch_uuid() -> None:
    assert expected_session_id("11111111-1111-4111-8111-111111111111") == (
        "cubeplex-agentcore-e3e6ae2cd3a325471ec97b9509e0438f638bedbba15e00f71d07ccdf50ca4437"
    )


@pytest.mark.asyncio
async def test_runtime_rejects_a_different_agentcore_session_before_control_plane_access() -> None:
    result = await invoke(
        {
            "version": 2,
            "dispatch_id": "11111111-1111-4111-8111-111111111111",
            "capability": "cap",
        },
        SimpleNamespace(session_id="wrong-session"),
    )
    assert result == {"status": "error", "error_code": "session_scope_mismatch"}


@pytest.mark.parametrize("version", [True, 1, 3])
def test_runtime_rejects_other_versions(version: object) -> None:
    with pytest.raises(NativeWorkerError, match="runtime_input_version_invalid"):
        _input(
            {
                "version": version,
                "dispatch_id": "11111111-1111-4111-8111-111111111111",
                "capability": "cap",
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_active_invocation_reports_busy_and_always_releases_health_task(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    import asyncio
    from cubeplex_native import runtime
    from cubeplex_native.client import NativeControlPlaneClient

    started, release = asyncio.Event(), asyncio.Event()

    class Client:
        async def aclose(self) -> None:
            if fails:
                raise RuntimeError("close failed")

    class Worker:
        def __init__(self, client: object) -> None:
            pass

        async def run(self) -> dict[str, str]:
            started.set()
            await release.wait()
            return {"status": "completed"}

    monkeypatch.setattr(NativeControlPlaneClient, "from_environment", lambda **_: Client())
    monkeypatch.setattr(runtime, "NativeWorker", Worker)
    dispatch_id = "11111111-1111-4111-8111-111111111111"
    task = asyncio.create_task(
        runtime.invoke(
            {"version": 2, "dispatch_id": dispatch_id, "capability": "task-only"},
            SimpleNamespace(session_id=expected_session_id(dispatch_id)),
        )
    )
    await started.wait()
    assert runtime.app.get_current_ping_status().value == "HealthyBusy"
    release.set()
    await task
    assert runtime.app.get_current_ping_status().value == "Healthy"
    assert runtime.app.get_async_task_info()["active_count"] == 0
