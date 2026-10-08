"""Поручения и подтверждения; historical schema independent of current models."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0002"
down_revision = "0001"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "tasks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "invocation_id", UUID, sa.ForeignKey("invocations.id"), unique=True, nullable=False
        ),
        sa.Column("operation_id", UUID, sa.ForeignKey("operations.id"), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_timezone", sa.String(), nullable=False),
        sa.Column("reference_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
    )
    op.create_index("ix_tasks_owner_id", "tasks", ["owner_id"])
    op.create_index("ix_tasks_deadline", "tasks", ["deadline"])
    op.create_table(
        "approvals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "invocation_id", UUID, sa.ForeignKey("invocations.id"), unique=True, nullable=False
        ),
        sa.Column("operation_id", UUID, sa.ForeignKey("operations.id"), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_approvals_owner_id", "approvals", ["owner_id"])


def downgrade():
    op.drop_table("approvals")
    op.drop_table("tasks")
