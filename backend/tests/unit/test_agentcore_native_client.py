"""Native invocation preserves AWS identity while narrowing the wire payload."""

from __future__ import annotations

import io
import json
from uuid import uuid4

import pytest

from cubeplex.agentcore.client import AgentCoreClient
from cubeplex.agentcore.dispatch import agentcore_session_id


@pytest.mark.asyncio
async def test_native_wire_has_only_dispatch_and_task_capability() -> None:
    seen: list[dict[str, object]] = []

    class ExternalAWS:
        def invoke_agent_runtime(self, **kwargs: object) -> dict[str, object]:
            seen.append(kwargs)
            return {"response": io.BytesIO(b'{"status":"accepted"}')}

    dispatch_id = uuid4()
    result = await AgentCoreClient(
        runtime_arn="arn:native",
        client=ExternalAWS(),
        payload_factory=lambda did: {
            "version": 2,
            "dispatch_id": str(did),
            "capability": "short-task-capability",
        },
    ).invoke(dispatch_id)
    assert result == {"status": "accepted"}
    assert seen[0]["runtimeSessionId"] == agentcore_session_id(dispatch_id)
    assert seen[0]["agentRuntimeArn"] == "arn:native"
    assert json.loads(seen[0]["payload"]) == {
        "version": 2,
        "dispatch_id": str(dispatch_id),
        "capability": "short-task-capability",
    }
