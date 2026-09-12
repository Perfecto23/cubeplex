"""Startup recovery contracts for remote dispatch ownership."""

from __future__ import annotations

import pytest

from cubeplex.streams import recovery


@pytest.mark.asyncio
async def test_remote_dispatch_lookup_error_fails_closed_in_agentcore_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cubeplex.agentcore.dispatch.active_dispatch_for_run",
        lambda *_args, **_kwargs: _raise_lookup_error(),
    )
    monkeypatch.setattr(recovery, "_agentcore_backend_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="dispatch lookup unavailable"):
        await recovery._has_active_remote_dispatch("run-1")


async def _raise_lookup_error() -> None:
    raise RuntimeError("dispatch lookup unavailable")
