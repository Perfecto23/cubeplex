"""Internal task callback receipts, committed with native checkpoint mutations."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class AgentCoreCallback(SQLModel, table=True):
    # Internal composite identity; no independently addressable business/public ID.
    __tablename__ = "agentcore_callbacks"

    dispatch_id: UUID = Field(foreign_key="agentcore_dispatches.id", primary_key=True)
    request_id: UUID = Field(primary_key=True)
    operation: str = Field(max_length=64)
    payload_sha256: str = Field(max_length=64)
    status: str = Field(default="pending", max_length=16)
    response: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
