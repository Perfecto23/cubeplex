"""Task-only Bearer capabilities, cryptographically separate from user login tokens."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from uuid import UUID

import jwt
from fastapi import Header

from cubeplex.agentcore.dispatch import agentcore_session_id
from cubeplex.agentcore.native_service import NativeTaskError
from cubeplex.config import config

PURPOSE = "cubeplex:agentcore-native-task:v1"


def _key() -> bytes:
    secret = str(config.get("auth.jwt_secret", ""))
    if len(secret) < 32 or secret.startswith("CHANGE_ME"):
        raise NativeTaskError("native_signing_unavailable", 503)
    return hmac.new(secret.encode(), PURPOSE.encode(), hashlib.sha256).digest()


def issue_capability(dispatch_id: UUID) -> str:
    now = int(datetime.now(UTC).timestamp())
    ttl = int(config.get("agentcore.capability_ttl_seconds", 1800))
    return jwt.encode(
        {
            "sub": str(dispatch_id),
            "sid": agentcore_session_id(dispatch_id),
            "aud": PURPOSE,
            "purpose": PURPOSE,
            "iat": now,
            "exp": now + ttl,
        },
        _key(),
        algorithm="HS256",
    )


def verify_capability(token: str, dispatch_id: UUID) -> None:
    try:
        claims = jwt.decode(
            token,
            _key(),
            algorithms=["HS256"],
            audience=PURPOSE,
            options={"require": ["sub", "sid", "aud", "purpose", "iat", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise NativeTaskError("native_capability_invalid", 401) from exc
    if (
        claims["sub"] != str(dispatch_id)
        or claims["purpose"] != PURPOSE
        or claims["sid"] != agentcore_session_id(dispatch_id)
    ):
        raise NativeTaskError("native_capability_scope_denied", 403)


async def require_capability(dispatch_id: UUID, authorization: str = Header(default="")) -> UUID:
    kind, _, token = authorization.partition(" ")
    if kind.lower() != "bearer" or not token or len(token) > 4096:
        raise NativeTaskError("native_capability_required", 401)
    verify_capability(token, dispatch_id)
    return dispatch_id
