"""canonical records table

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "records",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("record_type", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String()),
        sa.Column("profile_id", sa.String()),
        sa.Column("collection_id", sa.String()),
        sa.Column("agent_id", sa.String()),
        sa.Column("session_id", sa.String()),
        sa.Column("content_hash", sa.String()),
        sa.Column("status", sa.String()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "record_type",
            "tenant_id",
            "profile_id",
            "collection_id",
            "agent_id",
            "session_id",
            "content_hash",
            name="uq_observation_content",
        ),
    )
    for column in ("record_type", "tenant_id", "collection_id", "status"):
        op.create_index(f"ix_records_{column}", "records", [column])


def downgrade() -> None:
    op.drop_table("records")
