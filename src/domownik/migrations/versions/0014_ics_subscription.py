"""calendar subscription: per-user token hash and scope

Revision ID: 9a4d3c7b5e21
Revises: 2f8b6d4e1a93
Create Date: 2026-11-03 11:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a4d3c7b5e21"
down_revision: str | None = "2f8b6d4e1a93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ALTER TABLE ADD COLUMN + a unique index – the user table is not rebuilt.
    # (Autogenerate also offers to re-create the unnamed FKs of item.user_id / transaction.income_id –
    # older ondelete drift, left alone on purpose.)
    op.add_column("user", sa.Column("ics_token_hash", sa.String(length=64), nullable=True))
    op.add_column("user", sa.Column("ics_scope", sa.String(length=10), server_default="all", nullable=False))
    op.create_index("ix_user_ics_token_hash", "user", ["ics_token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_ics_token_hash", table_name="user")
    # SQLite ≥ 3.35 drops a column in place (ALTER TABLE … DROP COLUMN), no table rebuild
    op.drop_column("user", "ics_scope")
    op.drop_column("user", "ics_token_hash")
