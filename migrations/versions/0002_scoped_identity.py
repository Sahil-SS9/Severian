"""replace legacy observation uniqueness with scoped identities

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


IDENTITY_COLUMNS = [
    "record_type",
    "tenant_id",
    "profile_id",
    "collection_id",
    "agent_id",
    "session_id",
    "identity_key",
]
LEGACY_COLUMNS = [
    "record_type",
    "tenant_id",
    "profile_id",
    "collection_id",
    "agent_id",
    "session_id",
    "content_hash",
]


def upgrade() -> None:
    # SQLite cannot drop an inline unique constraint in place. Batch mode
    # recreates the table so databases that already applied 0001 converge
    # with clean installations.
    with op.batch_alter_table("records", recreate="always") as batch:
        batch.add_column(sa.Column("identity_key", sa.String(), nullable=True))
        batch.drop_constraint("uq_observation_content", type_="unique")
    op.create_index(
        "uq_records_scoped_identity",
        "records",
        IDENTITY_COLUMNS,
        unique=True,
        sqlite_where=sa.text("identity_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_records_scoped_identity", table_name="records")
    with op.batch_alter_table("records", recreate="always") as batch:
        batch.create_unique_constraint("uq_observation_content", LEGACY_COLUMNS)
        batch.drop_column("identity_key")
