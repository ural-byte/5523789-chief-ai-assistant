"""Activity, nullable latency and exact approved data controls."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004"
down_revision = "0003"
branch_labels = depends_on = None


def upgrade():
    for column in (
        "source_message_at",
        "received_at",
        "first_started_at",
        "processing_finished_at",
        "first_feedback_at",
        "final_delivery_ack_at",
    ):
        op.add_column("operations", sa.Column(column, sa.DateTime(timezone=True)))
    op.add_column(
        "operations", sa.Column("context_epoch", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("operations", sa.Column("retrieval_ms", sa.Integer()))
    op.add_column("operations", sa.Column("agent_loop_ms", sa.Integer()))
    op.add_column(
        "outbox", sa.Column("purpose", sa.String(), nullable=False, server_default="final")
    )
    op.add_column("outbox", sa.Column("acknowledged_at", sa.DateTime(timezone=True)))
    op.add_column("outbox", sa.Column("send_latency_ms", sa.Integer()))
    op.add_column(
        "approvals", sa.Column("action_kind", sa.String(), nullable=False, server_default="meeting")
    )
    op.execute(
        sa.text("UPDATE outbox SET purpose='notification' WHERE key LIKE :pattern").bindparams(
            pattern="task:%:reminder"
        )
    )
    op.execute("UPDATE outbox SET purpose='callback' WHERE kind='answerCallbackQuery'")
    op.create_table(
        "user_states",
        sa.Column("owner_id", sa.BigInteger(), primary_key=True),
        sa.Column("context_epoch", sa.Integer(), nullable=False),
    )
    op.create_table(
        "tombstones",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("approval_id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tombstones_owner_id", "tombstones", ["owner_id"])
    op.create_table(
        "file_intents",
        sa.Column("path", sa.String(), primary_key=True),
        sa.Column("operation_id", UUID, sa.ForeignKey("operations.id"), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_file_intents_operation_id", "file_intents", ["operation_id"])
    op.create_index("ix_file_intents_owner_id", "file_intents", ["owner_id"])
    op.execute("""INSERT INTO file_intents(path,operation_id,owner_id)
        SELECT operation_id::text || '.pdf', operation_id, owner_id FROM documents
        ON CONFLICT DO NOTHING""")
    op.create_table(
        "deletion_cleanups",
        sa.Column("approval_id", UUID, primary_key=True),
        sa.Column("paths", JSONB, nullable=False),
        sa.Column("remaining", JSONB, nullable=False),
        sa.Column("error_code", sa.String()),
    )
    op.create_table(
        "approval_audit",
        sa.Column("callback_id", sa.String(), primary_key=True),
        sa.Column("approval_id", UUID),
        sa.Column("operation_id", UUID, sa.ForeignKey("operations.id"), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "activities",
        sa.Column("operation_id", UUID, sa.ForeignKey("operations.id"), primary_key=True),
        sa.Column("next_typing_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("progress_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("progress_sent", sa.Boolean(), nullable=False),
        sa.Column("document", sa.Boolean(), nullable=False),
    )
    op.execute("""CREATE FUNCTION protect_approval_payload() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.payload IS DISTINCT FROM OLD.payload OR NEW.owner_id != OLD.owner_id
         OR NEW.action_kind != OLD.action_kind OR NEW.chat_id != OLD.chat_id
         OR NEW.operation_id != OLD.operation_id OR NEW.invocation_id != OLD.invocation_id THEN
        RAISE EXCEPTION 'approval immutable fields';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE TRIGGER approval_payload_immutable BEFORE UPDATE ON approvals
        FOR EACH ROW EXECUTE FUNCTION protect_approval_payload()""")


def downgrade():
    op.execute("DROP TRIGGER approval_payload_immutable ON approvals")
    op.execute("DROP FUNCTION protect_approval_payload()")
    for table in (
        "activities",
        "approval_audit",
        "deletion_cleanups",
        "file_intents",
        "tombstones",
        "user_states",
    ):
        op.drop_table(table)
    op.drop_column("approvals", "action_kind")
    for column in ("purpose", "acknowledged_at", "send_latency_ms"):
        op.drop_column("outbox", column)
    for column in (
        "source_message_at",
        "received_at",
        "first_started_at",
        "processing_finished_at",
        "first_feedback_at",
        "final_delivery_ack_at",
        "retrieval_ms",
        "agent_loop_ms",
        "context_epoch",
    ):
        op.drop_column("operations", column)
