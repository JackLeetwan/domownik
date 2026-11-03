"""bank import (bank_rule, bank_line) and the zone of a monthly income

Revision ID: 3d7a9c1e5b26
Revises: 8f2d6a4c0e93
Create Date: 2026-11-03 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3d7a9c1e5b26"
down_revision: str | None = "8f2d6a4c0e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bank_rule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pattern", sa.String(80), nullable=False, unique=True),
        sa.Column(
            "budget_category_id",
            sa.Integer(),
            sa.ForeignKey("budget_category.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "bank_line",
        sa.Column("ref", sa.String(40), primary_key=True),
        sa.Column("booked_on", sa.Date(), nullable=False),
        sa.Column("amount_gr", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("transaction_id", sa.Integer(), sa.ForeignKey("transaction.id", ondelete="SET NULL")),
        sa.Column("imported_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    # nullable column with an inline REFERENCES – SQLite adds it in place
    op.execute("ALTER TABLE recurring_income ADD COLUMN item_id INTEGER REFERENCES item (id) ON DELETE SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("recurring_income", recreate="always") as batch:
        batch.drop_column("item_id")
    op.drop_table("bank_line")
    op.drop_table("bank_rule")
