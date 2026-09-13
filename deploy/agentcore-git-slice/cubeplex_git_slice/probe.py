"""Report credential reachability without exposing credential values."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import boto3
from botocore.config import Config

from .client import BrokerClient, BrokerConfig, BrokerError

SENSITIVE = re.compile(
    r"SECRET|TOKEN|PASSWORD|CREDENTIAL|ACCESS_KEY|API_KEY|PRIVATE_KEY|DATABASE|VAULT|SUPABASE|GITHUB|OPENAI|ANTHROPIC|PGPASSWORD|SSH_AUTH_SOCK",
    re.I,
)
TASK_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
}


@dataclass(slots=True)
class ProbeResult:
    environment_keys: list[str]
    proc_environment_keys: list[str]
    credential_helper: str | None
    canary: str
    broker_reachability: str
    sensitive_environment_keys: list[str]
    sensitive_proc_keys: list[str]
    credential_files: list[str]
    aws_identity: str
    platform_master_surface: str
    constraint_checks: dict[str, str]
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def _candidates(value: str) -> list[str]:
    candidates = [value]
    try:
        nested = json.loads(value)
    except (ValueError, TypeError):
        return candidates
    pending = [(nested, 0)]
    while pending and len(candidates) < 1000:
        item, depth = pending.pop()
        if isinstance(item, str):
            candidates.append(item)
        elif depth < 8 and isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif depth < 8 and isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return candidates


def scan_values(values: Mapping[str, str], fingerprints: set[str]) -> dict[str, Any]:
    sensitive = sorted(key for key in values if SENSITIVE.search(key))
    forbidden = [key for key in sensitive if key not in TASK_KEYS]
    matched = sum(
        hashlib.sha256(candidate.encode()).hexdigest() in fingerprints
        for value in values.values()
        for candidate in _candidates(value)
    )
    return {
        "sensitive_keys": sensitive,
        "forbidden_keys": forbidden,
        "fingerprint_matches": matched,
    }


def read_proc(root: Path = Path("/proc")) -> tuple[dict[str, str], int, int]:
    values: dict[str, str] = {}
    readable = denied = 0
    for path in sorted(root.glob("[0-9]*/environ"))[:64]:
        try:
            raw = path.read_bytes()[:262144]
        except OSError:
            denied += 1
            continue
        readable += 1
        for part in raw.decode(errors="replace").split("\0"):
            if "=" in part:
                key, value = part.split("=", 1)
                # Keep every process's value while preserving the actual key for classification.
                previous = values.get(key)
                values[key] = value if previous is None else json.dumps([previous, value])
    return values, readable, denied


def expected_identity(arn: str, role_arn: str) -> bool:
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/(.+)", role_arn)
    if not match:
        return False
    role_name = match[2].rsplit("/", 1)[-1]
    return arn.startswith(f"arn:aws:sts::{match[1]}:assumed-role/{role_name}/")


async def run_probe(
    broker: BrokerClient, *, canary_secret_arn: str | None, check_constraints: bool = False
) -> ProbeResult:
    manifest: dict[str, Any] = {}
    try:
        manifest = await broker.manifest()
        reachability = "reachable"
    except Exception as exc:
        reachability = f"unreachable:{getattr(exc, 'code', type(exc).__name__)}"
    sdk_config = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1})
    canary = "not_configured"
    if canary_secret_arn:
        try:
            boto3.client(
                "secretsmanager", region_name=broker.config.region, config=sdk_config
            ).get_secret_value(SecretId=canary_secret_arn)
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            canary = (
                "iam_denied"
                if code == "AccessDeniedException"
                else f"unconfirmed:{type(exc).__name__}"
            )
        else:
            canary = "readable"
    identity = "unavailable"
    try:
        identity = boto3.client(
            "sts", region_name=broker.config.region, config=sdk_config
        ).get_caller_identity()["Arn"]
    except Exception:
        pass
    identity_ok = expected_identity(identity, str(manifest.get("worker_role_arn", "")))
    fingerprints = set(manifest.get("forbidden_value_sha256") or [])
    fingerprints.add(str(manifest.get("canary_sha256", "")))
    environment = scan_values(dict(os.environ), fingerprints)
    proc_values, readable, denied = read_proc()
    process_scan = scan_values(proc_values, fingerprints)
    paths = [
        Path.home() / ".aws/credentials",
        Path.home() / ".git-credentials",
        Path.home() / ".netrc",
        Path.home() / ".config/gh/hosts.yml",
        Path.home() / ".ssh/id_rsa",
        Path.home() / ".ssh/id_ed25519",
        Path("/app/.env"),
    ]
    credential_files = [str(path) for path in paths if path.exists()]
    helper: str | None = "unconfirmed"
    try:
        result = subprocess.run(
            ["git", "config", "--get-all", "credential.helper"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        helper = (
            "configured"
            if result.stdout.strip()
            else (None if result.returncode in (0, 1) else "unconfirmed")
        )
    except Exception:
        pass
    # Scan only application text, never return contents or matching values.
    app_matches = 0
    for path in sorted(Path("/app").glob("cubeplex_git_slice/*.py")):
        content = path.read_text(errors="replace")[:1048576]
        literals = re.findall(r"""["']([^"'\n]{8,8192})["']""", content)
        app_matches += sum(hashlib.sha256(v.encode()).hexdigest() in fingerprints for v in literals)
    surface_ok = (
        identity_ok
        and canary == "iam_denied"
        and readable > 0
        and helper is None
        and not credential_files
        and not environment["forbidden_keys"]
        and not process_scan["forbidden_keys"]
        and not environment["fingerprint_matches"]
        and not process_scan["fingerprint_matches"]
        and not app_matches
    )
    checks = {
        name: "not_run"
        for name in ("wrong_repo", "wrong_ref", "wrong_op", "wrong_capability", "budget_zero")
    }
    if check_constraints and reachability == "reachable":

        async def denial(awaitable: Any) -> str:
            try:
                await awaitable
            except BrokerError as exc:
                return exc.code
            except Exception as exc:
                return f"unconfirmed:{type(exc).__name__}"
            return "accepted"

        data = {
            "repo": manifest["repo"],
            "branch": manifest["branch"],
            "base_sha": manifest["base_sha"],
            "commit": "0" * 40,
            "bundle_b64": "",
        }
        checks["wrong_repo"] = await denial(broker.push({**data, "repo": "wrong/repo"}))
        checks["wrong_ref"] = await denial(broker.push({**data, "branch": "main"}))
        checks["wrong_op"] = await denial(broker.call("read_secret", {}))
        bad = BrokerClient(
            BrokerConfig(
                broker.config.function_arn,
                broker.config.region,
                broker.config.task_id,
                broker.config.stage,
                "wrong-capability",
            ),
            lambda_client=broker._lambda,
        )
        checks["wrong_capability"] = await denial(bad.manifest())
        if manifest.get("max_model_calls") == 0:
            checks["budget_zero"] = await denial(
                broker.model(
                    {
                        "model": manifest["model"],
                        "input": "probe",
                        "stream": True,
                        "store": False,
                        "max_output_tokens": 1,
                    }
                )
            )
        else:
            checks["budget_zero"] = "not_applicable"
    return ProbeResult(
        environment_keys=sorted(k for k in os.environ if not SENSITIVE.search(k)),
        proc_environment_keys=sorted(k for k in proc_values if not SENSITIVE.search(k)),
        credential_helper=helper,
        canary=canary,
        broker_reachability=reachability,
        sensitive_environment_keys=environment["sensitive_keys"],
        sensitive_proc_keys=process_scan["sensitive_keys"],
        credential_files=credential_files,
        aws_identity=identity,
        platform_master_surface="aws_task_role_or_capability_only" if surface_ok else "fail_closed",
        constraint_checks=checks,
        evidence={
            "identity_matches_task_role": identity_ok,
            "proc_readable": readable,
            "proc_denied": denied,
            "environment_forbidden_keys": environment["forbidden_keys"],
            "proc_forbidden_keys": process_scan["forbidden_keys"],
            "master_fingerprint_matches": environment["fingerprint_matches"]
            + process_scan["fingerprint_matches"]
            + app_matches,
            "task_credentials_may_be_readable": True,
        },
    )
