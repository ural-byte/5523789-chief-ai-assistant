"""Selective memory resolution and finite processing/delivery lifecycle."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0005"
down_revision = "0004"
branch_labels = depends_on = None


def upgrade():
    for name in ("deadline_at",):
        op.add_column("operations", sa.Column(name, sa.DateTime(timezone=True)))
    op.execute("""UPDATE operations SET deadline_at=coalesce(received_at,reference_at)+
        CASE WHEN scenario='pdf_index' OR EXISTS(SELECT 1 FROM jobs j
            WHERE j.operation_id=operations.id AND j.kind IN ('document','pdf_index'))
            THEN interval '15 minutes' ELSE interval '90 seconds' END""")
    op.alter_column("operations", "deadline_at", nullable=False)
    op.add_column(
        "operations",
        sa.Column("terminal_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "operations",
        sa.Column("delivery_state", sa.String(), nullable=False, server_default="pending"),
    )
    op.add_column("operations", sa.Column("error_reason", sa.String()))
    op.add_column(
        "operations",
        sa.Column("recovery_created", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("approvals", sa.Column("stale_reason", sa.String()))
    op.add_column("outbox", sa.Column("delivery_deadline_at", sa.DateTime(timezone=True)))
    op.execute("""UPDATE outbox b SET delivery_deadline_at=(CASE WHEN b.purpose='notification'
        THEN b.available_at ELSE coalesce(o.processing_finished_at,o.received_at,b.available_at)
        END)+interval '60 seconds' FROM operations o WHERE o.id=b.operation_id""")
    op.execute(
        "UPDATE outbox SET delivery_deadline_at=available_at+interval '60 seconds' "
        "WHERE delivery_deadline_at IS NULL"
    )
    op.alter_column("outbox", "delivery_deadline_at", nullable=False)
    op.add_column(
        "outbox", sa.Column("terminal_revision", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("outbox", sa.Column("failure_class", sa.String()))
    op.add_column("outbox", sa.Column("terminal_failed_at", sa.DateTime(timezone=True)))
    op.create_table(
        "memory_contexts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("operation_id", UUID, sa.ForeignKey("operations.id"), nullable=False),
        sa.Column(
            "invocation_id", UUID, sa.ForeignKey("invocations.id"), unique=True, nullable=False
        ),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("context_epoch", sa.Integer(), nullable=False),
        sa.Column("entries", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_contexts_operation_id", "memory_contexts", ["operation_id"])
    op.create_index("ix_memory_contexts_owner_id", "memory_contexts", ["owner_id"])
    op.create_table(
        "approval_previews",
        sa.Column("approval_id", UUID, primary_key=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
    )
    op.create_index("ix_approval_previews_owner_id", "approval_previews", ["owner_id"])
    op.execute("""CREATE FUNCTION protect_approval_preview() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'approval preview immutable'; END $$""")
    op.execute("""CREATE TRIGGER approval_preview_immutable BEFORE UPDATE ON approval_previews
        FOR EACH ROW EXECUTE FUNCTION protect_approval_preview()""")


def downgrade():
    op.execute("DROP TRIGGER approval_preview_immutable ON approval_previews")
    op.execute("DROP FUNCTION protect_approval_preview()")
    op.drop_table("approval_previews")
    op.drop_table("memory_contexts")
    for name in (
        "delivery_deadline_at",
        "terminal_revision",
        "failure_class",
        "terminal_failed_at",
    ):
        op.drop_column("outbox", name)
    op.drop_column("approvals", "stale_reason")
    for name in (
        "deadline_at",
        "terminal_revision",
        "delivery_state",
        "error_reason",
        "recovery_created",
    ):
        op.drop_column("operations", name)
