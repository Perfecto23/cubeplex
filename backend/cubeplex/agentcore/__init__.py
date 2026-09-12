"""AgentCore execution boundary for the CubePlex control plane.

Imports stay lazy so the Runtime entrypoint can load ``CONFIG_SECRET_ARN``
before CubePlex's Dynaconf settings and model registry are imported.
"""

__all__ = [
    "AgentCoreDispatchError",
    "AgentCoreClient",
    "AgentCoreInvocation",
    "AgentCoreWorker",
    "AgentCoreStopUnknown",
    "DispatchScope",
    "DispatchValidationError",
    "agentcore_session_id",
    "invocation_payload",
    "WorkerInvocationResult",
]


def __getattr__(name: str) -> object:
    if name in {
        "AgentCoreDispatchError",
        "AgentCoreInvocation",
        "AgentCoreStopUnknown",
        "DispatchScope",
        "DispatchValidationError",
        "agentcore_session_id",
        "invocation_payload",
    }:
        from cubeplex.agentcore import dispatch

        return getattr(dispatch, name)
    if name == "AgentCoreClient":
        from cubeplex.agentcore.client import AgentCoreClient

        return AgentCoreClient
    if name in {"AgentCoreWorker", "WorkerInvocationResult"}:
        from cubeplex.agentcore import worker

        return getattr(worker, name)
    raise AttributeError(name)
