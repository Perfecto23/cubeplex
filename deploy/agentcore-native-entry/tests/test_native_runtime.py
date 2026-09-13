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
