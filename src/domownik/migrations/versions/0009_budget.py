"""budget: envelope limits, archived categories, monthly incomes

Revision ID: 5c2e7a91d0b4
Revises: d8618a23c9a7
Create Date: 2026-11-03 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c2e7a91d0b4"
down_revision: str | None = "d8618a23c9a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD COLUMN only – no table rebuild, so nothing that references these tables is touched.
    op.execute("ALTER TABLE budget_category ADD COLUMN monthly_limit_gr INTEGER")
    op.execute("ALTER TABLE budget_category ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0")
    op.create_table(
        "recurring_income",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("amount_gr", sa.Integer(), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("budget_category_id", sa.Integer(), nullable=True),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["budget_category_id"], ["budget_category.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        'ALTER TABLE "transaction" ADD COLUMN income_id INTEGER REFERENCES recurring_income (id) ON DELETE SET NULL'
    )


def downgrade() -> None:
    # SQLite cannot DROP a column with a foreign key: rebuild with foreign keys off (see 0008).
    conn = op.get_bind()
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("Cannot switch foreign keys off inside a transaction – refusing to rebuild tables")
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.drop_column("income_id")
    op.drop_table("recurring_income")
    with op.batch_alter_table("budget_category", schema=None) as batch_op:
        batch_op.drop_column("archived")
        batch_op.drop_column("monthly_limit_gr")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
