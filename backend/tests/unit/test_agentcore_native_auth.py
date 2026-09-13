from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest

from cubeplex.agentcore import native_auth
from cubeplex.agentcore.native_service import NativeTaskError


def test_task_capability_is_dispatch_and_purpose_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "test-only-purpose-separated-key-12345678901234567890"
    monkeypatch.setattr(
        native_auth,
        "config",
        SimpleNamespace(
            get=lambda key, default=None: secret if key == "auth.jwt_secret" else default
        ),
    )
    dispatch = uuid4()
    token = native_auth.issue_capability(dispatch)
    native_auth.verify_capability(token, dispatch)
    with pytest.raises(NativeTaskError):
        native_auth.verify_capability(token, uuid4())
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(token, secret, algorithms=["HS256"], audience=native_auth.PURPOSE)
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["exp"] = int(datetime.now(UTC).timestamp()) - 1
    expired = jwt.encode(claims, native_auth._key(), algorithm="HS256")
    with pytest.raises(NativeTaskError):
        native_auth.verify_capability(expired, dispatch)


@pytest.mark.parametrize("value", ["", "short", "CHANGE_ME_IN_PRODUCTION_NOT_SECURE"])
def test_placeholder_keys_fail_closed(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        native_auth,
        "config",
        SimpleNamespace(
            get=lambda key, default=None: value if key == "auth.jwt_secret" else default
        ),
    )
    with pytest.raises(NativeTaskError):
        native_auth.issue_capability(uuid4())
