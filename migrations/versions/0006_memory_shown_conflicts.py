"""Bind conflict choices to the exact delivered pair."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = depends_on = None


def upgrade():
    op.add_column("memory_contexts", sa.Column("shown_conflicts", JSONB, nullable=True))


def downgrade():
    op.drop_column("memory_contexts", "shown_conflicts")
