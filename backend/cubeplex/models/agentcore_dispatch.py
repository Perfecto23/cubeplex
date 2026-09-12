"""Durable control-plane records for AgentCore execution.

The dispatch row is the hand-off boundary between the API process and the
AgentCore worker.  The request JSON is immutable after insertion; the worker
is allowed to update only claim/terminal metadata and the stop fence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, DateTime, Index, text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlmodel import Field, SQLModel

from cubeplex.models.mixins import OrgScopedMixin, TimestampMixin

AGENTCORE_DISPATCH_STATUSES = ("created", "claimed", "finished", "stop_unknown")
AGENTCORE_DISPATCH_OPERATIONS = ("prompt", "respond")


class AgentCoreDispatch(SQLModel, TimestampMixin, OrgScopedMixin, table=True):
    """One immutable invocation request owned by one AgentCore session.

    Prompt dispatches are unique by ``run_id``.  Respond dispatches are
    unique by the CAS ``claim_token`` returned by ``claim_resume``.  The
    partial indexes make those two idempotency contracts explicit in the
    database rather than relying on process-local task state.
    """

    _PREFIX: ClassVar[str] = "acdp"
    __tablename__ = "agentcore_dispatches"
    __table_args__ = (
        Index(
            "uq_agentcore_dispatch_prompt_run",
            "run_id",
            unique=True,
            postgresql_where=text("operation = 'prompt'"),
        ),
        Index(
            "uq_agentcore_dispatch_respond_claim",
            "claim_token",
            unique=True,
            postgresql_where=text("operation = 'respond'"),
        ),
        Index("ix_agentcore_dispatch_active_run", "run_id", "status"),
        Index("ix_agentcore_dispatch_scope", "org_id", "workspace_id", "conversation_id"),
    )

    id: UUID = Field(
        default_factory=lambda: uuid4(),
        sa_column=Column(PostgresUUID(as_uuid=True), nullable=False, primary_key=True),
    )
    run_id: str = Field(max_length=64, index=True)
    conversation_id: str = Field(foreign_key="conversations.id", max_length=20)
    user_id: str = Field(foreign_key="users.id", max_length=20)
    operation: str = Field(max_length=16)
    claim_token: str | None = Field(default=None, max_length=128, nullable=True)
    request: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="created", max_length=24, index=True)
    session_id: str = Field(max_length=128)
    stop_requested: bool = Field(
        default=False, nullable=False, sa_column_kwargs={"server_default": "false"}
    )
    claimed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    finished_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    heartbeat_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    error_code: str | None = Field(default=None, max_length=128, nullable=True)
    error_message: str | None = Field(default=None, nullable=True)

    def request_scope(self) -> tuple[str, str, str, str, str]:
        """Return the trusted scope tuple carried by the dispatch row."""
        return (
            self.org_id,
            self.workspace_id,
            self.conversation_id,
            self.user_id,
            self.run_id,
        )

    def touch_heartbeat(self) -> None:
        self.heartbeat_at = datetime.now(UTC)
