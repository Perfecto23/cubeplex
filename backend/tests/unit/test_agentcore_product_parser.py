"""Worker parser discovery and real text parser dispatch smoke."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cubeplex.agentcore.worker import AgentCoreWorker
from cubeplex.parsers import (
    ParseOptions,
    TextOutput,
    get_parser_registry,
    reset_parser_registry_for_tests,
)


@pytest.mark.asyncio
async def test_worker_discovers_entry_points_once_and_dispatches_text() -> None:
    reset_parser_registry_for_tests()
    worker = AgentCoreWorker(
        session_maker=MagicMock(),
        redis=MagicMock(),
        redis_key_prefix="p",
        manager_factory=AsyncMock(),
    )

    class Sandbox:
        async def download(self, _paths: list[str]) -> list[tuple[str, bytes]]:
            return [("/workspace/readme.txt", b"hello from worker\n")]

    try:
        await worker._ensure_runtime_dependencies()
        registry = get_parser_registry()
        await worker._ensure_runtime_dependencies()
        parsed = await registry.dispatch(
            Sandbox(),
            "/workspace/readme.txt",
            ParseOptions(),
            conversation_id=None,
        )
        assert isinstance(parsed, TextOutput)
        assert parsed.content == "hello from worker"
        assert parsed.metadata["parser"] == "text"
    finally:
        reset_parser_registry_for_tests()
