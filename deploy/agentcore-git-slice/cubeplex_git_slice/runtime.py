"""Standalone AgentCore Runtime entrypoint for the Git MicroVM slice."""

from __future__ import annotations

import os
import hashlib
import platform
import uuid
from pathlib import Path
from typing import Any, Mapping

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .client import BrokerClient, BrokerConfig, BrokerError
from .probe import run_probe
from .worker import GitSliceWorker, WorkerError

app = BedrockAgentCoreApp()


def _kernel_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return uuid.uuid4().hex


KERNEL_BOOT_ID = _kernel_boot_id()
BOOT_ID = hashlib.sha256(f"{platform.node()}:{KERNEL_BOOT_ID}".encode()).hexdigest()[:32]


def _safe_input(payload: Any) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise WorkerError("runtime_input_object_required")
    allowed = {"version", "task_id", "stage", "capability", "mode"}
    if set(payload) != allowed:
        raise WorkerError("runtime_input_shape_invalid")
    if type(payload.get("version")) is not int or payload.get("version") != 1:
        raise WorkerError("runtime_input_version_invalid")
    values = {key: payload.get(key) for key in allowed}
    if not all(
        isinstance(values[key], str) and values[key]
        for key in ("task_id", "stage", "capability", "mode")
    ):
        raise WorkerError("runtime_input_field_invalid")
    if values["stage"] not in {"work", "resume"} or values["mode"] not in {"probe", "run"}:
        raise WorkerError("runtime_input_mode_invalid")
    return {key: str(value) for key, value in values.items()}


def _broker(values: Mapping[str, str]) -> BrokerClient:
    return BrokerClient(
        BrokerConfig(
            function_arn=os.environ["BROKER_FUNCTION_ARN"],
            region=os.environ.get("AWS_REGION", "us-west-2"),
            task_id=values["task_id"],
            stage=values["stage"],
            capability=values["capability"],
        )
    )


async def invoke(payload: Any, context: Any = None) -> dict[str, Any]:
    values = _safe_input(payload)
    broker = _broker(values)
    session_id = getattr(context, "session_id", None)
    workspace = Path(os.environ.get("CUBEPLEX_WORKSPACE", "/workspace/task"))
    workspace_preexisting = workspace.exists()
    try:
        if values["mode"] == "probe":
            manifest = await broker.manifest()
            probe = await run_probe(
                broker,
                canary_secret_arn=manifest.get("canary_secret_arn"),
                check_constraints=True,
            )
            return {
                "status": "probe",
                "boot_id": BOOT_ID,
                "session_id": session_id,
                "hostname": platform.node(),
                "kernel_boot_id": KERNEL_BOOT_ID,
                "workspace_preexisting": workspace_preexisting,
                "probe": probe.to_dict(),
            }

        task_id = app.add_async_task("agentcore-git-slice", {"stage": values["stage"]})
        worker = GitSliceWorker(
            broker=broker,
            workspace=workspace,
            boot_id=BOOT_ID,
        )
        result = await worker.run(values["stage"])
        return {
            "status": result.get("status", "complete"),
            "boot_id": BOOT_ID,
            "session_id": session_id,
            "hostname": platform.node(),
            "kernel_boot_id": KERNEL_BOOT_ID,
            "workspace_preexisting": workspace_preexisting,
            **result,
        }
    except BrokerError as exc:
        return {"status": "error", "boot_id": BOOT_ID, "error_code": exc.code}
    except WorkerError as exc:
        return {
            "status": "error",
            "boot_id": BOOT_ID,
            "error_code": str(exc).split(":", 1)[0],
        }
    except Exception:
        return {"status": "error", "boot_id": BOOT_ID, "error_code": "worker_failed"}
    finally:
        if "task_id" in locals():
            app.complete_async_task(task_id)


app.entrypoint(invoke)


def main() -> None:
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
