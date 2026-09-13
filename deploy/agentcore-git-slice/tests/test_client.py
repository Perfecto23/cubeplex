from __future__ import annotations

import io
import json

import pytest

from cubeplex_git_slice.client import BrokerClient, BrokerConfig, BrokerError


class FakeLambda:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.kwargs: dict = {}

    def invoke(self, **kwargs):
        self.kwargs = kwargs
        return {"Payload": io.BytesIO(json.dumps(self.payload).encode())}


@pytest.mark.asyncio
async def test_broker_envelope_and_result() -> None:
    fake = FakeLambda({"ok": True, "result": {"repo": "fixture"}})
    client = BrokerClient(
        BrokerConfig(
            "arn:aws:lambda:us-west-2:1:function:broker", "us-west-2", "task", "work", "cap"
        ),
        lambda_client=fake,
        request_id_factory=lambda: "request",
    )
    result = await client.call("manifest", {})
    assert result == {"repo": "fixture"}
    envelope = json.loads(fake.kwargs["Payload"])
    assert envelope == {
        "version": 1,
        "task_id": "task",
        "stage": "work",
        "capability": "cap",
        "request_id": "request",
        "op": "manifest",
        "data": {},
    }


@pytest.mark.asyncio
async def test_broker_error_is_safe_code() -> None:
    fake = FakeLambda({"ok": False, "error": {"code": "capability_invalid", "detail": "secret"}})
    client = BrokerClient(
        BrokerConfig("arn", "us-west-2", "task", "work", "cap"), lambda_client=fake
    )
    with pytest.raises(BrokerError, match="capability_invalid"):
        await client.call("manifest", {})
