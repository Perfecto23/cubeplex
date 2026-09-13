from __future__ import annotations

import json

import httpx
import pytest

from cubeplex_native.checkpointer import RemoteCheckpointer
from cubeplex_native.client import ControlPlaneError, NativeControlPlaneClient


@pytest.mark.asyncio
async def test_client_binds_capability_and_checkpointer_restores_typed_messages() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer capability"
        body = json.loads(await request.aread()) if request.content else {}
        if request.url.path.endswith("/claim"):
            return httpx.Response(
                200,
                json={
                    "version": 1,
                    "result": {
                        "owner": True,
                        "dispatch_id": "11111111-1111-4111-8111-111111111111",
                        "run_id": "run-1",
                        "conversation_id": "conversation-1",
                        "session_id": "session-1",
                        "operation": "prompt",
                        "content": "hello",
                        "model": {"id": "fixture", "max_output_tokens": 64},
                    },
                },
            )
        if request.url.path.endswith("/checkpoint") and body["op"] == "load":
            return httpx.Response(
                200,
                json={
                    "version": 1,
                    "result": {
                        "messages": [
                            {
                                "role": "user",
                                "content": [{"type": "text", "text": "old"}],
                                "run_id": "run-0",
                            }
                        ],
                        "extra": {"answer": 1},
                    },
                },
            )
        return httpx.Response(200, json={"version": 1, "result": {}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://cp.test")
    client = NativeControlPlaneClient(
        base_url="https://cp.test",
        dispatch_id="11111111-1111-4111-8111-111111111111",
        capability="capability",
        http_client=http,
    )
    claim = await client.claim()
    checkpointer = RemoteCheckpointer(client, "conversation-1")
    loaded = await checkpointer.load("conversation-1")
    await client.aclose()

    assert claim["owner"] is True
    assert loaded is not None
    assert loaded.messages[0].role == "user"
    assert loaded.extra == {"answer": 1}
    assert calls[0].url.path.endswith("/claim")


@pytest.mark.asyncio
async def test_events_use_monotonic_seq_and_raw_model_response() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/model/responses"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"data: response.completed\n\n",
            )
        return httpx.Response(200, json={"version": 1, "result": {"seq": 1}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://cp.test")
    client = NativeControlPlaneClient(
        base_url="https://cp.test",
        dispatch_id="11111111-1111-4111-8111-111111111111",
        capability="capability",
        http_client=http,
    )
    await client.events(1, {"type": "agent_start"})
    response = await client.model_response(
        {"model": "fixture"}, "22222222-2222-4222-8222-222222222222"
    )
    await client.aclose()

    assert response.status_code == 200
    assert json.loads(seen[0].content) == {
        "version": 1,
        "seq": 1,
        "event": {"type": "agent_start"},
    }
    assert seen[1].headers["x-agentcore-request-id"] == "22222222-2222-4222-8222-222222222222"


def test_client_requires_an_https_origin_without_path_query_or_fragment() -> None:
    kwargs = {
        "dispatch_id": "11111111-1111-4111-8111-111111111111",
        "capability": "capability",
    }
    for url in (
        "http://cp.test",
        "https://cp.test/api",
        "https://cp.test?token=bad",
        "https://cp.test#fragment",
    ):
        with pytest.raises(ValueError):
            NativeControlPlaneClient(base_url=url, **kwargs)


@pytest.mark.asyncio
async def test_client_rejects_success_response_without_v1_result_envelope() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://cp.test")
    client = NativeControlPlaneClient(
        base_url="https://cp.test",
        dispatch_id="11111111-1111-4111-8111-111111111111",
        capability="capability",
        http_client=http,
    )
    with pytest.raises(ControlPlaneError, match="control_plane_envelope_invalid"):
        await client.control()
    await client.aclose()
