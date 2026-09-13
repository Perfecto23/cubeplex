"""OpenAI Responses provider routed through the control plane."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

import httpx
from cubeloop.providers.openai_responses import OpenAIResponsesProvider
from openai import AsyncOpenAI

from .client import NativeControlPlaneClient


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str
    max_output_tokens: int = 2048


class ControlPlaneTransport(httpx.AsyncBaseTransport):
    """Keep model HTTP inside the fixed, capability-authenticated origin."""

    def __init__(self, client: NativeControlPlaneClient) -> None:
        self.client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.method != "POST"
            or str(request.url) != "https://native-model.invalid/v1/responses"
        ):
            return httpx.Response(400, json={"error": "model_route_invalid"}, request=request)
        try:
            body = json.loads(await request.aread())
        except (TypeError, json.JSONDecodeError):
            return httpx.Response(400, json={"error": "request_json_invalid"}, request=request)
        if not isinstance(body, dict):
            return httpx.Response(400, json={"error": "request_object_required"}, request=request)
        request_id = request.headers.get("x-agentcore-request-id") or str(uuid4())
        try:
            response = await self.client.model_response(body, request_id)
        except Exception as exc:
            code = getattr(exc, "code", "model_proxy_failed")
            return httpx.Response(502, json={"error": code}, request=request)
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    async def aclose(self) -> None:
        return


class NativeOpenAIResponsesProvider(OpenAIResponsesProvider):
    """Official CubeLoop Responses loop with no model credential in the VM."""

    def __init__(self, client: NativeControlPlaneClient, config: ModelConfig) -> None:
        super().__init__(
            api_key="native-control-plane-placeholder",
            base_url="https://native-model.invalid/v1",
            provider_id="agentcore.native.responses",
        )
        self._client = AsyncOpenAI(
            api_key="native-control-plane-placeholder",
            base_url="https://native-model.invalid/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=ControlPlaneTransport(client),
                trust_env=False,
                timeout=httpx.Timeout(90.0, connect=5.0),
            ),
        )
        self.config = config

    async def aclose(self) -> None:
        await self._client.close()
