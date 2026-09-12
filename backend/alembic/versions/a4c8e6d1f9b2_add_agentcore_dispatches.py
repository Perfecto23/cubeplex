"""add durable AgentCore dispatch records

Revision ID: a4c8e6d1f9b2
Revises: a20bbb53ccff
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a4c8e6d1f9b2"
down_revision: str | None = "a20bbb53ccff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agentcore_dispatches",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", sa.String(length=20), nullable=False),
        sa.Column("workspace_id", sa.String(length=20), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=20), nullable=False),
        sa.Column("user_id", sa.String(length=20), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("claim_token", sa.String(length=128), nullable=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="created"),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("stop_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_agentcore_dispatch_prompt_run",
        "agentcore_dispatches",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("operation = 'prompt'"),
    )
    op.create_index(
        "uq_agentcore_dispatch_respond_claim",
        "agentcore_dispatches",
        ["claim_token"],
        unique=True,
        postgresql_where=sa.text("operation = 'respond'"),
    )
    op.create_index(
        "ix_agentcore_dispatch_active_run",
        "agentcore_dispatches",
        ["run_id", "status"],
    )
    op.create_index(
        "ix_agentcore_dispatch_scope",
        "agentcore_dispatches",
        ["org_id", "workspace_id", "conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agentcore_dispatch_scope", table_name="agentcore_dispatches")
    op.drop_index("ix_agentcore_dispatch_active_run", table_name="agentcore_dispatches")
    op.drop_index("uq_agentcore_dispatch_respond_claim", table_name="agentcore_dispatches")
    op.drop_index("uq_agentcore_dispatch_prompt_run", table_name="agentcore_dispatches")
    op.drop_table("agentcore_dispatches")
