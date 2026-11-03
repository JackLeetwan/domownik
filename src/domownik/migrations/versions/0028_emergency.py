"""'Na wypadek': emergency_entry, emergency_print

Revision ID: 9d4b6e1a3f57
Revises: 7c1f3a9d2e48
Create Date: 2026-11-03 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d4b6e1a3f57"
down_revision: str | None = "7c1f3a9d2e48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "emergency_entry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("section", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "emergency_print",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("printed_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("emergency_print")
    op.drop_table("emergency_entry")
