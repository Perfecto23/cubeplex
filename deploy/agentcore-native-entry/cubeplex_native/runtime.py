"""Bedrock AgentCore Runtime entrypoint for native CubePlex execution."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping
from uuid import UUID

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .client import ControlPlaneError
from .worker import NativeWorker, NativeWorkerError

app = BedrockAgentCoreApp()


def expected_session_id(dispatch_id: str) -> str:
    parsed = UUID(dispatch_id)
    return f"cubeplex-agentcore-{hashlib.sha256(parsed.bytes).hexdigest()}"


def _input(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, Mapping) or set(payload) != {"version", "dispatch_id", "capability"}:
        raise NativeWorkerError("runtime_input_shape_invalid")
    if type(payload.get("version")) is not int or payload.get("version") != 2:
        raise NativeWorkerError("runtime_input_version_invalid")
    dispatch_id = payload.get("dispatch_id")
    capability = payload.get("capability")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        raise NativeWorkerError("dispatch_id_invalid")
    if not isinstance(capability, str) or not capability:
        raise NativeWorkerError("capability_invalid")
    return dispatch_id, capability


async def invoke(payload: Any, context: Any = None) -> dict[str, Any]:
    dispatch_id, capability = _input(payload)
    session_id = getattr(context, "session_id", None)
    try:
        expected = expected_session_id(dispatch_id)
    except (ValueError, AttributeError, TypeError):
        return {"status": "error", "error_code": "dispatch_id_invalid"}
    if session_id != expected:
        return {"status": "error", "error_code": "session_scope_mismatch"}
    try:
        from .client import NativeControlPlaneClient

        client = NativeControlPlaneClient.from_environment(
            dispatch_id=dispatch_id,
            capability=capability,
        )
        try:
            result = await NativeWorker(client).run()
        finally:
            await client.aclose()
        return {
            **result,
            "session_id": session_id,
        }
    except ControlPlaneError as exc:
        return {"status": "error", "error_code": exc.code}
    except NativeWorkerError as exc:
        return {"status": "error", "error_code": str(exc)}
    except Exception:
        return {"status": "error", "error_code": "worker_failed"}


app.entrypoint(invoke)


def main() -> None:
    app.run(host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
