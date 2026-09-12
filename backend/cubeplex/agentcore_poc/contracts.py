"""Narrow, shared wire and authorization contracts for the AgentCore PoC."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class InvocationRequest(StrictModel):
    schema_version: Literal["1"]
    run_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    team_id: str = Field(pattern=r"^T[A-Z0-9]{8,20}$")
    channel_id: str = Field(pattern=r"^[CG][A-Z0-9]{8,20}$")
    thread_ts: str = Field(pattern=r"^[0-9]{10,12}\.[0-9]{6}$")
    user_id: str = Field(pattern=r"^U[A-Z0-9]{8,20}$")
    prompt: str = Field(min_length=1, max_length=12000)
    repository: str = Field(min_length=3, max_length=150)


class ScopeDenied(ValueError):
    """The request does not belong to the explicitly configured PoC scope."""


class RuntimeScope(StrictModel):
    team_id: str
    channel_id: str
    user_ids: frozenset[str]
    repository: Literal["Perfecto23/corplink-rs"] = "Perfecto23/corplink-rs"

    @classmethod
    def from_env(cls) -> RuntimeScope:
        return cls(
            team_id=os.environ["CUBEPLEX_POC_ALLOWED_TEAM_ID"],
            channel_id=os.environ["CUBEPLEX_POC_ALLOWED_CHANNEL_ID"],
            user_ids=frozenset(
                user.strip()
                for user in os.environ["CUBEPLEX_POC_ALLOWED_USER_IDS"].split(",")
                if user.strip()
            ),
            repository=os.environ["CUBEPLEX_POC_ALLOWED_REPOSITORY"],  # type: ignore[arg-type]
        )

    def authorize(self, request: InvocationRequest) -> None:
        if (
            request.team_id != self.team_id
            or request.channel_id != self.channel_id
            or request.user_id not in self.user_ids
            or request.repository != self.repository
        ):
            raise ScopeDenied("scope_denied")


def derive_runtime_session_id(request: InvocationRequest | Mapping[str, object]) -> str:
    parsed = (
        request
        if isinstance(request, InvocationRequest)
        else InvocationRequest.model_validate(request)
    )
    scope = [parsed.team_id, parsed.channel_id, parsed.thread_ts, parsed.user_id, parsed.repository]
    digest = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
    return f"cubeplex-poc-{digest}"


class ProviderSecret(StrictModel):
    base_url: str
    api_key: SecretStr
    model: str = Field(min_length=1, max_length=150)
    effort: Literal["minimal", "low", "medium", "high", "max"] = "low"


class Evidence(StrictModel):
    path: str
    start_line: int
    end_line: int
    blob_sha: str
    excerpt: str


RunStatus = Literal[
    "completed", "incomplete", "denied", "invalid_request", "timeout", "cancelled", "internal_error"
]


class InvocationResponse(StrictModel):
    schema_version: Literal["1"] = "1"
    run_id: str = ""
    runtime_session_id: str = ""
    status: RunStatus
    repository: str = ""
    commit_sha: str | None = None
    answer: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    model: str | None = None
    error_code: str | None = None
