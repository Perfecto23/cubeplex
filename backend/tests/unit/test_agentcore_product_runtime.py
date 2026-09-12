"""AgentCore Runtime protocol and config bootstrap contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cubeplex.agentcore import runtime


def test_config_secret_accepts_only_dynaconf_environment_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.get_secret_value.return_value = {
        "SecretString": '{"ENV_FOR_DYNACONF":"production","CUBEPLEX_REDIS__URL":"redis://redis"}'
    }
    monkeypatch.setattr(runtime.boto3, "client", lambda name: client)
    monkeypatch.setenv("CONFIG_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:123:secret:cfg")

    loaded = runtime.bootstrap_config_secret()

    assert loaded["ENV_FOR_DYNACONF"] == "production"
    assert loaded["CUBEPLEX_REDIS__URL"] == "redis://redis"
    client.get_secret_value.assert_called_once()
    monkeypatch.delenv("ENV_FOR_DYNACONF", raising=False)
    monkeypatch.delenv("CUBEPLEX_REDIS__URL", raising=False)


def test_config_secret_rejects_arbitrary_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": '{"PYTHONPATH":"/tmp"}'}
    monkeypatch.setattr(runtime.boto3, "client", lambda name: client)

    with pytest.raises(ValueError, match="key_not_allowed"):
        runtime.bootstrap_config_secret(
            environ={"CONFIG_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:123:secret:cfg"}
        )


@pytest.mark.asyncio
async def test_runtime_handler_rejects_without_configured_worker() -> None:
    result = await runtime.invoke(
        {"version": 1, "dispatch_id": "00000000-0000-0000-0000-000000000000"},
        SimpleNamespace(session_id="cubeplex-agentcore-deadbeef"),
    )
    assert result["error_code"] == "worker_not_configured"


def test_subprocess_bootstrap_precedes_real_worker_factory() -> None:
    """The import boundary must not freeze Dynaconf before Secret bootstrap."""
    import os
    import subprocess
    import sys
    import textwrap

    source = textwrap.dedent(
        """
        import json
        import os
        import boto3

        from cryptography.fernet import Fernet

        class SecretClient:
            def get_secret_value(self, *, SecretId):
                assert SecretId == "arn:secret:config"
                return {"SecretString": json.dumps({
                    "ENV_FOR_DYNACONF": "production",
                    "CUBEPLEX_DATABASE__HOST": "from-secret-host",
                    "CUBEPLEX_DATABASE__NAME": "from-secret-db",
                    "CUBEPLEX_AUTH__JWT_SECRET": "j" * 48,
                    "CUBEPLEX_AUTH__VAULT_KEY": Fernet.generate_key().decode(),
                    "CUBEPLEX_SANDBOX__ENABLED": "false",
                })}

        boto3.client = lambda _name: SecretClient()

        import redis.asyncio
        redis.asyncio.Redis.from_url = classmethod(lambda cls, *a, **k: object())

        from cubeplex.agentcore.runtime import build_default_worker
        assert "cubeplex.config" not in __import__("sys").modules
        worker = build_default_worker()
        from cubeplex.config import config
        assert worker is not None
        assert config.current_env == "production"
        assert config.get("database.host") == "from-secret-host"
        assert config.get("database.name") == "from-secret-db"
        """
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CUBEPLEX_") and key != "ENV_FOR_DYNACONF"
    }
    env["CONFIG_SECRET_ARN"] = "arn:secret:config"
    env["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
    result = subprocess.run(
        [sys.executable, "-c", source],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
