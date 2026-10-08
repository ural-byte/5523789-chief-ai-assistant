"""Structured facts, semantic memory and PDF index."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID

revision = "0003"
down_revision = "0002"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "memory_entries",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "invocation_id", UUID, sa.ForeignKey("invocations.id"), nullable=False, unique=True
        ),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("original", sa.String(), nullable=False),
        sa.Column("source_text", sa.String(), nullable=False),
        sa.Column("source_update_id", sa.BigInteger()),
        sa.Column("embedding", Vector(256), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_entries_owner_id", "memory_entries", ["owner_id"])
    op.create_table(
        "entities",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.UniqueConstraint("owner_id", "name"),
    )
    op.create_table(
        "facts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("entry_id", UUID, sa.ForeignKey("memory_entries.id"), nullable=False),
        sa.Column("entity_id", UUID, sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("predicate", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
    )
    op.create_table(
        "documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "operation_id", UUID, sa.ForeignKey("operations.id"), unique=True, nullable=False
        ),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("pages", sa.Integer()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_code", sa.String()),
    )
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])
    op.create_table(
        "document_chunks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("embedding", Vector(256), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.UniqueConstraint("document_id", "page", "ordinal"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index("ix_document_chunks_owner_id", "document_chunks", ["owner_id"])


def downgrade():
    for table in ["document_chunks", "documents", "facts", "entities", "memory_entries"]:
        op.drop_table(table)
