"""Actual pinned Responses provider -> broker -> external model boundary."""

from __future__ import annotations

import json
from importlib.metadata import version
from typing import Any

import httpx
import pytest
from cubeloop import Agent, ReasoningControl, tool
from cubeloop.providers.openai_responses import OpenAIResponsesProvider
from openai import AsyncOpenAI

from test_broker import event, make_broker, model_request


def _external_sse(*, call_tool: bool) -> bytes:
    item: dict[str, Any]
    if call_tool:
        item = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "shell",
            "arguments": '{"command":"test-only-command"}',
            "status": "completed",
        }
    else:
        item = {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Verified", "annotations": []}],
        }
    events = [
        {
            "type": "response.output_item.added",
            "sequence_number": 0,
            "output_index": 0,
            "item": item,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 1,
            "output_index": 0,
            "item": item,
        },
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": {"id": "resp_1", "status": "completed", "usage": None, "output": [item]},
        },
    ]
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()


@pytest.mark.parametrize("reasoning", [False, True])
async def test_real_native_provider_request_and_tool_replay_pass_broker(reasoning: bool) -> None:
    assert version("cubeloop") == "0.14.1"
    assert version("openai") == "2.40.0"
    requests: list[dict[str, Any]] = []
    broker_responses: list[dict[str, Any]] = []
    external_calls = 0
    executions: list[str] = []

    def external_model(_: httpx.Request) -> httpx.Response:
        nonlocal external_calls
        external_calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_external_sse(call_tool=external_calls == 1),
        )

    broker, external_requests = make_broker(external_model)

    async def route_to_real_broker(request: httpx.Request) -> httpx.Response:
        # This body is serialized by AsyncOpenAI from the native CubeLoop
        # provider, not a hand-written approximation of its request schema.
        body = json.loads(await request.aread())
        requests.append(body)
        response = broker.handle(event("model", {"body": body}))
        broker_responses.append(response)
        if not response["ok"]:
            return httpx.Response(502, json={"error": response["error"]}, request=request)
        result = response["result"]
        return httpx.Response(
            result["status"],
            headers=result["headers"],
            content=result["body"].encode(),
            request=request,
        )

    class InProcessBrokerTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await route_to_real_broker(request)

    @tool(execution_mode="sequential")
    async def shell(command: str) -> str:
        """Pure test tool; no real shell command is executed."""
        executions.append(command)
        return "test-output"

    provider = OpenAIResponsesProvider(
        provider_id="broker-test",
        api_key="non-secret-placeholder",
        base_url="https://broker.invalid/v1",
    )
    await provider._client.close()
    provider._client = AsyncOpenAI(
        api_key="non-secret-placeholder",
        base_url="https://broker.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=InProcessBrokerTransport()),
    )
    try:
        agent = Agent(
            model=provider.model("fixed-model", reasoning=reasoning, max_tokens=2048),
            reasoning=ReasoningControl(mode="auto", effort="low", summary="auto"),
            system_prompt="Use the registered test tool, then answer.",
            tools=[shell],
        )
        await agent.prompt("Call the test tool.", run_id="native-run")
        assert all(response["ok"] for response in broker_responses), broker_responses
        assert agent.state.last_outcome == "complete"
        assert executions == ["test-only-command"]
        assert len(requests) == len(external_requests) == 2
        assert broker.store.public_status()["model_calls"] == 2
        assert set(requests[0]) == {
            "model",
            "input",
            "stream",
            "store",
            "tools",
            "max_output_tokens",
        } | ({"reasoning", "include"} if reasoning else {"temperature"})
        for sent, forwarded in zip(requests, external_requests, strict=True):
            assert json.loads(forwarded.content) == sent
            assert sent["model"] == "fixed-model"
            assert sent["max_output_tokens"] == 2048
            assert sent["stream"] is True and sent["store"] is False
            assert sent["tools"][0]["type"] == "function"
            assert sent["tools"][0]["name"] == "shell"
        if reasoning:
            assert requests[0]["include"] == ["reasoning.encrypted_content"]
            assert requests[0]["reasoning"] == {"effort": "low", "summary": "auto"}
            assert "temperature" not in requests[0]
        else:
            assert "include" not in requests[0]
            assert requests[0]["temperature"] == 0.7
        replay = requests[1]["input"]
        assert any(item.get("type") == "function_call" for item in replay)
        assert any(item.get("type") == "function_call_output" for item in replay)
    finally:
        await provider._client.close()
        broker.http.close()


@pytest.mark.parametrize(
    "include",
    [
        None,
        "reasoning.encrypted_content",
        ["web_search_call.action.sources"],
        ["reasoning.encrypted_content", "message.input_image.image_url"],
        ["reasoning.encrypted_content", "reasoning.encrypted_content"],
    ],
)
def test_include_does_not_expand_to_other_provider_surfaces(include: Any) -> None:
    broker, calls = make_broker()
    assert not broker.handle(event("model", model_request(include=include)))["ok"]
    assert not calls and broker.store.public_status()["model_calls"] == 0
