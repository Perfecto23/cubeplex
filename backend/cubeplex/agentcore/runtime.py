"""Official AgentCore Runtime entrypoint for the CubePlex worker."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import boto3

try:  # The production image supplies the official SDK; unit tests may not.
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
    from bedrock_agentcore.runtime.context import RequestContext
except ImportError:  # pragma: no cover - exercised only in dependency-free tests
    BedrockAgentCoreApp = None  # type: ignore[assignment,misc]
    RequestContext = Any  # type: ignore[misc,assignment]


def bootstrap_config_secret(*, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Load the optional config secret without evaluating arbitrary settings.

    Only ``ENV_FOR_DYNACONF`` and ``CUBEPLEX_`` keys are accepted.  Secret
    contents cannot overwrite the ARN itself or any unrelated process setting.
    The caller invokes this before importing ``cubeplex.config``.
    """
    source = dict(environ or os.environ)
    arn = source.get("CONFIG_SECRET_ARN", "").strip()
    if not arn:
        return {}
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=arn)
    raw = response.get("SecretString")
    if not isinstance(raw, str):
        raise ValueError("agentcore_config_secret_string_required")
    values = json.loads(raw)
    if not isinstance(values, dict):
        raise ValueError("agentcore_config_secret_object_required")
    accepted: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not (
            key == "ENV_FOR_DYNACONF" or key.startswith("CUBEPLEX_")
        ):
            raise ValueError("agentcore_config_secret_key_not_allowed")
        if not isinstance(value, str):
            raise ValueError("agentcore_config_secret_value_must_be_string")
        accepted[key] = value
    os.environ.update(accepted)
    return accepted


app = BedrockAgentCoreApp() if BedrockAgentCoreApp is not None else None
_worker: Any | None = None


def configure_worker(worker: Any) -> None:
    """Install the bootstrapped worker before serving Runtime requests."""
    global _worker
    _worker = worker


def build_default_worker() -> Any:
    """Build the worker-only RunManager without starting the API lifespan.

    This deliberately initializes only dependencies consumed by a native run:
    credential encryption, MCP signing, Redis streams, sandbox manager and the
    shared checkpointer.  IM connectors, schedules, cleanup loops and startup
    recovery belong to the Kubernetes API process.
    """
    bootstrap_config_secret()
    from loguru import logger
    from redis.asyncio import Redis

    from cubeplex.agentcore.worker import AgentCoreWorker
    from cubeplex.audit.sink import NoOpAuditSink
    from cubeplex.config import config
    from cubeplex.credentials.encryption import FernetBackend
    from cubeplex.credentials.keys import parse_vault_keys
    from cubeplex.db.engine import async_session_maker
    from cubeplex.mcp.dependencies import build_user_token_signer
    from cubeplex.plugins import ensure_registry_bound
    from cubeplex.services.conversation_search.lexical import build_lexical_backend
    from cubeplex.streams.run_manager import RunManager
    from cubeplex.utils import log

    log.init()
    ensure_registry_bound()
    raw_vault_key = os.getenv("CUBEPLEX_AUTH__VAULT_KEY") or config.get("auth.vault_key")
    if not raw_vault_key:
        raise RuntimeError("agentcore_worker_vault_key_required")
    encryption_backend = FernetBackend(parse_vault_keys(str(raw_vault_key)))
    lexical_backend = build_lexical_backend()
    embedding_provider = None
    if config.get("search.embedding.enabled", False):
        try:
            from cubeplex.services.conversation_search.embedding import EmbeddingProvider

            embedding_provider = EmbeddingProvider.from_config()
        except RuntimeError as exc:
            logger.warning("AgentCore worker embedding provider unavailable: {}", exc)
    redis = Redis.from_url(
        config.get("redis.url", "redis://localhost:6379/0"),
        decode_responses=True,
        max_connections=config.get("redis.max_connections", 64),
        socket_timeout=config.get("redis.socket_timeout_seconds", 10),
        socket_connect_timeout=config.get("redis.socket_connect_timeout_seconds", 5),
    )
    state = SimpleNamespace(
        agentcore_worker=True,
        encryption_backend=encryption_backend,
        mcp_user_token_signer=build_user_token_signer(),
        tracer=None,
        audit_sink=NoOpAuditSink(),
        redis=redis,
        user_event_bus=None,
        lexical_backend=lexical_backend,
        embedding_provider=embedding_provider,
    )
    worker_app = SimpleNamespace(state=state)
    base_prefix = str(config.get("redis.key_prefix", "cubeplex"))
    env_name = os.getenv("ENV_FOR_DYNACONF", "development")
    key_prefix = f"{base_prefix}:{env_name}"
    manager = RunManager(
        app=worker_app,  # type: ignore[arg-type]
        redis=redis,
        key_prefix=key_prefix,
        run_event_ttl_seconds=int(config.get("streaming.run_event_ttl_seconds", 43200)),
        run_stream_max_events=int(config.get("streaming.run_stream_max_events", 1000000)),
    )

    if config.get("sandbox.enabled") is True:
        from cubeplex.sandbox.manager import init_sandbox_manager

        init_sandbox_manager(async_session_maker, encryption_backend)

    async def manager_factory(_: Any) -> Any:
        return manager

    return AgentCoreWorker(
        session_maker=async_session_maker,
        redis=redis,
        redis_key_prefix=key_prefix,
        manager_factory=manager_factory,
        run_event_ttl_seconds=int(config.get("streaming.run_event_ttl_seconds", 43200)),
    )


async def invoke(payload: Any, context: RequestContext) -> dict[str, object]:
    """Validate the Runtime session and delegate to the durable worker."""
    if not isinstance(payload, Mapping):
        return {"version": 1, "status": "invalid_request", "error_code": "payload_object_required"}
    if _worker is None:
        return {"version": 1, "status": "internal_error", "error_code": "worker_not_configured"}
    session_id = str(getattr(context, "session_id", "") or "")
    try:
        from cubeplex.agentcore.dispatch import DispatchValidationError

        result = await _worker.invoke(payload, session_id=session_id)
        return cast(dict[str, object], result.model_dump())
    except DispatchValidationError as exc:
        return {
            "version": 1,
            "status": "denied",
            "error_code": str(exc),
        }
    except asyncio.CancelledError:
        raise
    except Exception:
        return {
            "version": 1,
            "status": "internal_error",
            "error_code": "worker_failed",
        }


if app is not None:
    app.entrypoint(invoke)


def main() -> None:
    if app is None:
        raise RuntimeError("bedrock_agentcore_sdk_not_installed")
    configure_worker(build_default_worker())
    app.run(host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
