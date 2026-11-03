"""savings goals + transaction.goal_id

Revision ID: 1f6c8e4b9d32
Revises: e3a9f1c7b250
Create Date: 2026-11-03 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1f6c8e4b9d32"
down_revision: str | None = "e3a9f1c7b250"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "savings_goal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("icon", sa.String(length=16), nullable=True),
        sa.Column("target_gr", sa.Integer(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("done_on", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # nullable column with an inline REFERENCES – SQLite adds it without rebuilding the table
    op.execute('ALTER TABLE "transaction" ADD COLUMN goal_id INTEGER REFERENCES savings_goal (id) ON DELETE SET NULL')
    op.create_index("ix_transaction_goal_id", "transaction", ["goal_id"])


def downgrade() -> None:
    op.drop_index("ix_transaction_goal_id", table_name="transaction")
    with op.batch_alter_table("transaction", recreate="always") as batch:
        batch.drop_column("goal_id")
    op.drop_table("savings_goal")
