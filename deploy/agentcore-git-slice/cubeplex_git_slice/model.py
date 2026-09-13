"""OpenAI Responses compatibility layer backed by the broker Lambda."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI
from cubeloop.providers.openai_responses import (
    OpenAIResponsesProvider as _OpenAIResponsesProvider,
)

from .client import BrokerClient


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str
    max_output_tokens: int = 2048


class LambdaTransport(httpx.AsyncBaseTransport):
    """Translate an OpenAI Responses HTTP request into broker ``model``."""

    def __init__(self, broker: BrokerClient) -> None:
        self._broker = broker

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.method != "POST"
            or str(request.url) != "https://lambda-broker.invalid/v1/responses"
        ):
            return httpx.Response(400, json={"error": "model_route_invalid"}, request=request)
        body = await request.aread()
        try:
            decoded = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            return httpx.Response(400, json={"error": "request_json_invalid"}, request=request)
        if not isinstance(decoded, dict):
            return httpx.Response(400, json={"error": "request_object_required"}, request=request)
        try:
            result = await self._broker.model(decoded)
        except Exception as exc:
            code = getattr(exc, "code", "broker_model_failed")
            return httpx.Response(502, json={"error": code}, request=request)
        status = int(result.get("status", 502))
        headers = result.get("headers") or {"content-type": "text/event-stream"}
        content = result.get("body", "")
        if not isinstance(content, str):
            return httpx.Response(502, json={"error": "model_body_invalid"}, request=request)
        return httpx.Response(status, headers=headers, content=content.encode(), request=request)

    async def aclose(self) -> None:
        return


class BrokerOpenAIResponsesProvider(_OpenAIResponsesProvider):
    """Construct the official CubeLoop provider with a private HTTP client."""

    def __init__(
        self,
        broker: BrokerClient,
        config: ModelConfig,
    ) -> None:
        super().__init__(
            api_key="microvm-placeholder",
            base_url="https://lambda-broker.invalid/v1",
            provider_id="broker.openai.responses",
        )
        self._client = AsyncOpenAI(
            api_key="microvm-placeholder",
            base_url="https://lambda-broker.invalid/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=LambdaTransport(broker), trust_env=False),
        )
