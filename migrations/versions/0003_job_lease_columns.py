"""add job lease and retry columns to records

Revision ID: 0003
Revises: 0002

P0-4 added leased job claims (``lease_until``, ``retry_after``) to the
job/outbox rebuild. Fresh installs create the columns via
``metadata.create_all``; this migration brings existing databases up to
the same schema so the versioned lifecycle matches clean installs.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("records", recreate="always") as batch:
        batch.add_column(sa.Column("lease_until", sa.String(), nullable=True))
        batch.add_column(sa.Column("retry_after", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("records", recreate="always") as batch:
        batch.drop_column("retry_after")
        batch.drop_column("lease_until")
