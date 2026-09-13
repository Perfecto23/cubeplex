"""Standard Git remote-helper bridge to the broker push operation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import asyncio
import base64
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from .client import BrokerClient, BrokerConfig


def _config() -> dict[str, Any]:
    path = os.environ.get("CUBEPLEX_GIT_TASK_CONFIG", "")
    if not path:
        raise RuntimeError("git_task_config_required")
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise RuntimeError("git_task_config_invalid")
    return data


def _client(config: dict[str, Any]) -> BrokerClient:
    return BrokerClient(
        BrokerConfig(
            function_arn=str(config["broker_function_arn"]),
            region=str(config["region"]),
            task_id=str(config["task_id"]),
            stage=str(config["stage"]),
            capability=str(config["capability"]),
        )
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:512])
    return result.stdout.strip()


def _validate_refspec(config: dict[str, Any], source: str, destination: str) -> None:
    if (
        destination != f"refs/heads/{config['branch']}"
        or not source
        or source.startswith(("+", "-"))
        or any(char.isspace() for char in source)
        or ":" in source
    ):
        raise RuntimeError("push_refspec_denied")


async def _push(
    repo: Path, config: dict[str, Any], source: str, destination: str
) -> dict[str, Any]:
    _validate_refspec(config, source, destination)
    commit = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{source}^{{commit}}")
    base = str(config["base_sha"])
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (base, commit)):
        raise RuntimeError("push_commit_invalid")
    # A positive raw SHA range has no advertised ref and git bundle rejects
    # it as empty. A private temporary ref exports this exact existing commit.
    bundle_ref = f"refs/agentcore-bundle/{uuid4().hex}"
    _git(repo, "update-ref", bundle_ref, commit, "0" * 40)
    with tempfile.NamedTemporaryFile(prefix="git-slice-", suffix=".bundle", delete=False) as f:
        bundle_path = Path(f.name)
    try:
        subprocess.run(
            ["git", "bundle", "create", str(bundle_path), f"{base}..{bundle_ref}"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        bundle = bundle_path.read_bytes()
    finally:
        bundle_path.unlink(missing_ok=True)
        _git(repo, "update-ref", "-d", bundle_ref, commit)
    result = await _client(config).push(
        {
            "repo": config["repo"],
            "branch": config["branch"],
            "base_sha": base,
            "commit": commit,
            "bundle_b64": base64.b64encode(bundle).decode(),
        }
    )
    return {"commit": commit, **result}


def main() -> int:
    config = _config()
    repo = Path.cwd()
    pushes: list[str] = []
    for line in sys.stdin:
        command = line.strip()
        if not command:
            if pushes:
                # Git submits a complete push batch before reading statuses.
                # This slice permits one destination, so reject multi-ref
                # batches before any broker request can mutate remote state.
                for refspec in pushes:
                    parts = refspec.split(":")
                    source, destination = parts if len(parts) == 2 else ("", "invalid")
                    try:
                        if len(pushes) != 1:
                            raise RuntimeError("push_batch_denied")
                        asyncio.run(_push(repo, config, source, destination))
                    except Exception:
                        print(f"error {destination} broker_push_failed", flush=True)
                    else:
                        print(f"ok {destination}", flush=True)
                print(flush=True)
            return 0
        if command == "capabilities":
            print("push", flush=True)
            print(flush=True)
            continue
        if command == "list" or command == "list for-push":
            print(f"? refs/heads/{config['branch']}", flush=True)
            print(f"@refs/heads/{config['branch']} HEAD", flush=True)
            print(flush=True)
            continue
        if command.startswith("push "):
            pushes.append(command[5:])
            continue
        if command == "quit":
            return 0
        if command.startswith("option "):
            print("unsupported", flush=True)
            continue
        print("unsupported", flush=True)
    return 1 if pushes else 0


if __name__ == "__main__":
    raise SystemExit(main())
