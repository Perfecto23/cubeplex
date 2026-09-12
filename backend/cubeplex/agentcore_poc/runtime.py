"""AgentCore HTTP entrypoint; scope is checked before secrets, GitHub or model access."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from botocore.config import Config
from pydantic import ValidationError

from cubeplex.agentcore_poc.agent import run_agent
from cubeplex.agentcore_poc.contracts import (
    InvocationRequest,
    InvocationResponse,
    ProviderSecret,
    RuntimeScope,
    ScopeDenied,
    derive_runtime_session_id,
)

app = BedrockAgentCoreApp()


def load_provider_secret() -> ProviderSecret:
    arn = os.environ["CUBEPLEX_POC_PROVIDER_SECRET_ARN"]
    if (
        re.fullmatch(r"arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+", arn)
        is None
    ):
        raise ValueError("invalid_provider_secret_arn")
    client = boto3.Session().client(
        "secretsmanager",
        config=Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"total_max_attempts": 2, "mode": "standard"},
        ),
    )
    try:
        response = client.get_secret_value(SecretId=arn)
        return ProviderSecret.model_validate_json(response["SecretString"])
    finally:
        client.close()


@app.entrypoint
async def invoke(payload: Any, context: RequestContext) -> dict[str, Any]:
    session_id = context.session_id or ""
    try:
        request = InvocationRequest.model_validate(payload)
    except ValidationError:
        return InvocationResponse(
            status="invalid_request", error_code="invalid_request"
        ).model_dump()
    try:
        RuntimeScope.from_env().authorize(request)
        if session_id != derive_runtime_session_id(request):
            raise ScopeDenied("session_scope_mismatch")
    except ScopeDenied:
        return InvocationResponse(
            run_id=request.run_id,
            runtime_session_id=session_id,
            status="denied",
            error_code="scope_denied",
        ).model_dump()
    except (KeyError, ValidationError):
        return InvocationResponse(
            run_id=request.run_id, status="internal_error", error_code="scope_not_configured"
        ).model_dump()
    try:
        secret = await asyncio.to_thread(load_provider_secret)
        response = await run_agent(request, session_id, secret)
        return response.model_dump()
    except asyncio.CancelledError:
        raise
    except Exception:
        # Do not let SDK exception logging print provider bodies, URLs, or secrets.
        return InvocationResponse(
            run_id=request.run_id,
            runtime_session_id=session_id,
            status="internal_error",
            repository=request.repository,
            error_code="runtime_failed",
        ).model_dump()


def main() -> None:
    app.run(host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
