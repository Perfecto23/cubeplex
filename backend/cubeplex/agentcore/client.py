"""Small synchronous boto3 adapter used from async RunManager code."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config

from cubeplex.agentcore.dispatch import agentcore_session_id, invocation_payload


class AgentCoreClient:
    """IAM-authenticated AgentCore Runtime client with retries disabled."""

    def __init__(
        self,
        *,
        runtime_arn: str,
        region_name: str | None = None,
        client: Any | None = None,
        payload_factory: Callable[[UUID], Mapping[str, object]] | None = None,
    ) -> None:
        if not runtime_arn:
            raise ValueError("agentcore_runtime_arn_required")
        self.runtime_arn = runtime_arn
        self._payload_factory = payload_factory or invocation_payload
        self._client = client or boto3.Session(region_name=region_name).client(
            "bedrock-agentcore",
            region_name=region_name,
            config=Config(
                connect_timeout=10,
                read_timeout=310,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )

    async def invoke(self, dispatch_id: UUID) -> Mapping[str, object]:
        """Invoke one dispatch; never retry an uncertain network outcome."""
        session_id = agentcore_session_id(dispatch_id)
        payload = json.dumps(self._payload_factory(dispatch_id), separators=(",", ":")).encode()
        response = await asyncio.to_thread(
            self._client.invoke_agent_runtime,
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=payload,
        )
        body = response.get("response")
        if body is None:
            return {}
        try:
            raw = await asyncio.to_thread(body.read, 512_001)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        if len(raw) > 512_000:
            raise ValueError("agentcore_response_too_large")
        if not raw:
            return {}
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("agentcore_response_invalid")
        return decoded

    async def stop(self, dispatch_id: UUID) -> Mapping[str, object]:
        """Request bounded AgentCore session teardown for one dispatch."""
        return await asyncio.to_thread(
            self._client.stop_runtime_session,
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId=agentcore_session_id(dispatch_id),
        )
