"""Bounded Responses relay; model credentials stay in the trusted Backend."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlmodel import col

from cubeplex.agentcore.native_service import (
    NativeTaskError,
    callback_transaction,
    canonical,
    load_native_dispatch,
    payload_hash,
)
from cubeplex.config import config
from cubeplex.db.engine import async_session_maker
from cubeplex.llm.resolver import parse_model_ref
from cubeplex.llm.snapshot import load_llm_snapshot
from cubeplex.models.agentcore_callback import AgentCoreCallback
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch

FIELDS = {
    "model",
    "input",
    "instructions",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "max_output_tokens",
    "reasoning",
    "temperature",
    "top_p",
    "stream",
    "store",
    "text",
    "include",
}


def validate_model_body(body: dict[str, Any], manifest: dict[str, Any]) -> None:
    if set(body) - FIELDS or len(canonical(body)) > 262144:
        raise NativeTaskError("native_model_request_denied", 422)
    if (
        body.get("model") != manifest["id"]
        or body.get("stream") is not True
        or body.get("store") is not False
    ):
        raise NativeTaskError("native_model_request_denied", 422)
    tokens = body.get("max_output_tokens")
    if type(tokens) is not int or not 1 <= tokens <= min(int(manifest["max_output_tokens"]), 2048):
        raise NativeTaskError("native_model_output_limit", 422)
    if "include" in body and body["include"] not in ([], ["reasoning.encrypted_content"]):
        raise NativeTaskError("native_model_include_denied", 422)
    tools = body.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 32:
        raise NativeTaskError("native_model_tools_denied", 422)
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or tool.get("type") != "function"
            or set(tool) - {"type", "name", "description", "parameters", "strict"}
        ):
            raise NativeTaskError("native_model_tools_denied", 422)
    inputs = body.get("input")
    if not isinstance(inputs, (str, list)):
        raise NativeTaskError("native_model_input_denied", 422)
    for item in inputs if isinstance(inputs, list) else []:
        if not isinstance(item, dict) or item.get("type", "message") not in {
            "message",
            "function_call",
            "function_call_output",
            "reasoning",
        }:
            raise NativeTaskError("native_model_input_denied", 422)
        content = item.get("content", [])
        if isinstance(content, list) and any(
            not isinstance(block, dict)
            or block.get("type") not in {"input_text", "output_text", "refusal"}
            for block in content
        ):
            raise NativeTaskError("native_model_input_denied", 422)


def complete_sse(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
        normalized = text.replace("\r\n", "\n")
        if not normalized.endswith("\n\n"):
            raise ValueError("truncated")
        complete = False
        for packet in normalized.split("\n\n"):
            data = "\n".join(
                line[5:].lstrip(" ") for line in packet.splitlines() if line.startswith("data:")
            )
            if not data:
                continue
            if data == "[DONE]" and complete:
                continue
            event = json.loads(data)
            if complete or event.get("type") in {"error", "response.failed", "response.incomplete"}:
                raise ValueError("failed")
            if event.get("type") == "response.completed":
                if event.get("response", {}).get("status") != "completed":
                    raise ValueError("not completed")
                complete = True
        if not complete:
            raise ValueError("missing terminal")
        return text
    except (ValueError, UnicodeError, AttributeError) as exc:
        raise NativeTaskError("native_model_response_invalid", 502) from exc


async def _upstream(body: dict[str, Any], base_url: str, api_key: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise NativeTaskError("native_model_configuration_invalid", 503)
    try:
        async with (
            asyncio.timeout(120),
            httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=httpx.Timeout(90, connect=10)
            ) as client,
        ):
            async with client.stream(
                "POST",
                base_url.rstrip("/") + "/responses",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"},
            ) as response:
                if response.status_code != 200:
                    raise NativeTaskError("native_model_rejected", 502)
                if "text/event-stream" not in response.headers.get("content-type", "").lower():
                    raise NativeTaskError("native_model_response_invalid", 502)
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > 2 * 1024 * 1024:
                        raise NativeTaskError("native_model_response_too_large", 502)
        return complete_sse(bytes(raw))
    except NativeTaskError:
        raise
    except Exception as exc:
        raise NativeTaskError("native_model_outcome_unknown", 502) from exc


async def proxy_model(
    dispatch_id: UUID, request_id: UUID, body: dict[str, Any], *, encryption_backend: Any
) -> str:
    async with callback_transaction(dispatch_id, request_id, "model", body) as tx:
        if tx.replay:
            result = tx.result or {}
            if "error_code" in result:
                raise NativeTaskError(result["error_code"], 502)
            return str(result["body"])
        d = tx.dispatch
        validate_model_body(body, d.request["model"])
        count = await tx.session.scalar(
            select(func.count())
            .select_from(AgentCoreCallback)
            .join(
                AgentCoreDispatch, col(AgentCoreDispatch.id) == col(AgentCoreCallback.dispatch_id)
            )
            .where(
                col(AgentCoreDispatch.run_id) == d.run_id,
                col(AgentCoreCallback.operation) == "model",
            )
        )
        # Pending receipt is autoflushed by the count and belongs to this call.
        if int(count or 0) > int(config.get("agentcore.native_max_model_calls", 100)):
            raise NativeTaskError("native_model_budget_exceeded", 429)
        slug, model_id = parse_model_ref(str(d.request["model_ref"]))
        snapshot = await load_llm_snapshot(tx.session, d.org_id, encryption_backend)
        provider = snapshot.providers.get(slug)
        if (
            provider is None
            or provider.api != "openai-responses"
            or not provider.api_key
            or not any(model.id == model_id for model in provider.models)
            or model_id != d.request["model"]["id"]
        ):
            raise NativeTaskError("native_model_configuration_invalid", 503)
        base_url, api_key = provider.base_url, provider.api_key
        # Exit without complete(): persist pending admission BEFORE HTTP. A
        # process crash leaves it unknown, so a retry cannot charge another call.
    outcome: dict[str, Any]
    try:
        text = await _upstream(body, base_url, api_key)
        outcome = {"body": text}
    except NativeTaskError as exc:
        outcome = {"error_code": exc.code}
    async with async_session_maker() as session, session.begin():
        d = await load_native_dispatch(session, dispatch_id, for_update=True)
        receipt = await session.get(AgentCoreCallback, (dispatch_id, request_id))
        if receipt is None or receipt.payload_sha256 != payload_hash(
            {"operation": "model", "payload": body}
        ):
            raise NativeTaskError("native_callback_outcome_unknown")
        if d.stop_requested or d.status != "claimed":
            raise NativeTaskError("native_task_stopped")
        receipt.response, receipt.status = outcome, "done"
        receipt.completed_at = datetime.now(UTC)
        session.add(receipt)
    if "error_code" in outcome:
        raise NativeTaskError(outcome["error_code"], 502)
    return str(outcome["body"])
