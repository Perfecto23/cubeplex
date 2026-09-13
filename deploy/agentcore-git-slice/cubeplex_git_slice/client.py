"""Strict broker client used by the MicroVM worker and Git remote helper."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import boto3
from botocore.config import Config


class BrokerError(RuntimeError):
    """A broker request was rejected or could not be decoded safely."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    function_arn: str
    region: str
    task_id: str
    stage: str
    capability: str


class BrokerClient:
    """One-shot Lambda broker client with retries disabled at both layers."""

    def __init__(
        self,
        config: BrokerConfig,
        *,
        lambda_client: Any | None = None,
        request_id_factory: Any = uuid.uuid4,
    ) -> None:
        if not config.function_arn:
            raise ValueError("broker_function_arn_required")
        self.config = config
        self._request_id_factory = request_id_factory
        self._lambda = lambda_client or boto3.Session(region_name=config.region).client(
            "lambda",
            region_name=config.region,
            config=Config(
                connect_timeout=5,
                read_timeout=130,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )

    def _private_call(self, envelope: dict[str, Any]) -> dict[str, Any]:
        raw = self._lambda.invoke(
            FunctionName=self.config.function_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(envelope, separators=(",", ":")).encode(),
        )
        if raw.get("FunctionError"):
            raise BrokerError("broker_function_error")
        payload = raw.get("Payload")
        if payload is None:
            raise BrokerError("broker_empty_response")
        try:
            body = payload.read(6 * 1024 * 1024 + 1)
        finally:
            close = getattr(payload, "close", None)
            if close is not None:
                close()
        if len(body) > 6 * 1024 * 1024:
            raise BrokerError("broker_response_too_large")
        try:
            response = json.loads(body)
        except (TypeError, json.JSONDecodeError) as exc:
            raise BrokerError("broker_invalid_response") from exc
        if (
            not isinstance(response, dict)
            or set(response) != {"ok", "result"}
            and set(response) != {"ok", "error"}
        ):
            raise BrokerError("broker_response_shape_invalid")
        if response.get("ok") is True:
            result = response.get("result")
            if not isinstance(result, dict):
                raise BrokerError("broker_result_shape_invalid")
            return result
        error = response.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise BrokerError(str(code or "broker_rejected"))

    async def call(self, op: str, data: Mapping[str, Any]) -> dict[str, Any]:
        envelope = {
            "version": 1,
            "task_id": self.config.task_id,
            "stage": self.config.stage,
            "capability": self.config.capability,
            "request_id": str(self._request_id_factory()),
            "op": op,
            "data": dict(data),
        }
        return await asyncio.to_thread(self._private_call, envelope)

    async def manifest(self) -> dict[str, Any]:
        return await self.call("manifest", {})

    async def model(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self.call("model", {"body": dict(body)})

    async def push(self, data: Mapping[str, Any]) -> dict[str, Any]:
        return await self.call("push", data)

    async def create_pull_request(self, data: Mapping[str, Any]) -> dict[str, Any]:
        return await self.call("pr", data)

    async def checkpoint_put(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        return await self.call("checkpoint_put", {"snapshot": dict(snapshot)})

    async def checkpoint_get(self) -> dict[str, Any]:
        return await self.call("checkpoint_get", {})

    async def status(self) -> dict[str, Any]:
        return await self.call("status", {})
