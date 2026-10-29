"""Shaping steps: each dataset keeps its cleaned upload and the steps applied to it.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    with op.batch_alter_table("datasets") as batch:  # batch mode so SQLite (local dev) can add the foreign key too
        batch.add_column(sa.Column("base_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("steps", json, nullable=False, server_default="[]"))
        batch.create_index("ix_datasets_base_hash", ["base_hash"])
        batch.create_foreign_key("datasets_base_hash_fkey", "dataset_blobs", ["base_hash"], ["content_hash"])


def downgrade() -> None:
    with op.batch_alter_table("datasets") as batch:
        batch.drop_constraint("datasets_base_hash_fkey", type_="foreignkey")
        batch.drop_index("ix_datasets_base_hash")
        batch.drop_column("steps")
        batch.drop_column("base_hash")
