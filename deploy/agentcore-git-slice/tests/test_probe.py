from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from cubeplex_git_slice import probe


class _ProbeBroker:
    def __init__(self, manifest: dict):
        self.config = SimpleNamespace(
            function_arn="arn:broker", region="us-west-2", task_id="t", stage="work"
        )
        self._lambda = MagicMock()
        self._manifest = manifest

    async def manifest(self):
        return self._manifest


def _denied() -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue")


def _clients(monkeypatch: pytest.MonkeyPatch, *, canary: object, role: str) -> None:
    class Secrets:
        def get_secret_value(self, **_kwargs):
            if isinstance(canary, BaseException):
                raise canary
            return {"SecretString": "test-secret"}

    class Sts:
        def get_caller_identity(self):
            return {"Arn": role}

    monkeypatch.setattr(
        probe.boto3,
        "client",
        lambda service, **_kwargs: Secrets() if service == "secretsmanager" else Sts(),
    )


def _manifest(*, role: str, fingerprints: list[str] | None = None) -> dict:
    return {
        "repo": "Perfecto23/cubeplex-microvm-git-poc-20260913",
        "remote_url": "https://github.com/Perfecto23/cubeplex-microvm-git-poc-20260913.git",
        "base_sha": "0" * 40,
        "branch": "agentcore/fix-inclusive-total",
        "model": "fixture-model",
        "worker_role_arn": role,
        "canary_sha256": hashlib.sha256(b"test-secret").hexdigest(),
        "forbidden_value_sha256": fingerprints or [],
        "max_model_calls": 20,
    }


def _clean_surface(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(probe.os, "environ", {"SAFE_VM_FLAG": "1"})


@pytest.mark.asyncio
async def test_probe_accepts_exact_task_role_and_canary_denial_without_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clean_surface(monkeypatch, tmp_path)
    role = "arn:aws:iam::123456789012:role/GitSliceWorker"
    _clients(
        monkeypatch,
        canary=_denied(),
        role="arn:aws:sts::123456789012:assumed-role/GitSliceWorker/session",
    )
    monkeypatch.setattr(probe, "read_proc", lambda: ({}, 1, 0))
    monkeypatch.setattr(
        probe.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="", returncode=1)
    )
    result = await probe.run_probe(
        _ProbeBroker(_manifest(role=role)),
        canary_secret_arn="arn:canary",
    )
    output = json.dumps(result.to_dict())
    assert result.canary == "iam_denied"
    assert result.platform_master_surface == "aws_task_role_or_capability_only"
    assert result.evidence["identity_matches_task_role"] is True
    assert "test-secret" not in output
    assert "arn:canary" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["readable", "network", "wrong_role", "helper", "sensitive"])
async def test_probe_fail_closed_for_untrusted_surfaces(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    tmp_path,
) -> None:
    _clean_surface(monkeypatch, tmp_path)
    role = "arn:aws:iam::123456789012:role/GitSliceWorker"
    canary: object = _denied()
    returned_role = "arn:aws:sts::123456789012:assumed-role/GitSliceWorker/session"
    if failure == "readable":
        canary = "readable"
    elif failure == "network":
        canary = RuntimeError("network_secret_value")
    elif failure == "wrong_role":
        returned_role = "arn:aws:sts::123456789012:assumed-role/OtherRole/session"
    _clients(monkeypatch, canary=canary, role=returned_role)
    monkeypatch.setattr(probe, "read_proc", lambda: ({}, 1, 0))
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="helper-secret-value" if failure == "helper" else "",
            returncode=0 if failure == "helper" else 1,
        ),
    )
    if failure == "sensitive":
        probe.os.environ["CUBEPLEX_GITHUB_TOKEN"] = "platform-secret-value"
    result = await probe.run_probe(
        _ProbeBroker(_manifest(role=role)),
        canary_secret_arn="arn:canary",
    )
    assert result.platform_master_surface == "fail_closed"
    assert "platform-secret-value" not in json.dumps(result.to_dict())


@pytest.mark.asyncio
async def test_probe_detects_nested_fingerprint_and_proc_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clean_surface(monkeypatch, tmp_path)
    role = "arn:aws:iam::123456789012:role/GitSliceWorker"
    secret = "nested-platform-secret"
    _clients(
        monkeypatch,
        canary=_denied(),
        role="arn:aws:sts::123456789012:assumed-role/GitSliceWorker/session",
    )
    monkeypatch.setattr(
        probe, "read_proc", lambda: ({"APP_CONFIG": json.dumps({"nested": secret})}, 1, 0)
    )
    monkeypatch.delenv("CUBEPLEX_GITHUB_TOKEN", raising=False)
    result = await probe.run_probe(
        _ProbeBroker(
            _manifest(role=role, fingerprints=[hashlib.sha256(secret.encode()).hexdigest()])
        ),
        canary_secret_arn="arn:canary",
    )
    assert result.platform_master_surface == "fail_closed"
    assert result.evidence["master_fingerprint_matches"] >= 1
    assert secret not in json.dumps(result.to_dict())
