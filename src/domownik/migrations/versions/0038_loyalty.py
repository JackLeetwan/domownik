"""loyalty cards of the house, shown at the till from the shopping list (🛒 → 💳)

Revision ID: 5d2f8a6c3e17
Revises: 3c7a9e1b5d84
Create Date: 2026-11-10 22:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d2f8a6c3e17"
down_revision: str | None = "3c7a9e1b5d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "loyalty_card",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("code", sa.String(120), nullable=False),
        sa.Column("fmt", sa.String(20), nullable=False),
        sa.Column("note", sa.String(120), nullable=True),
        sa.Column("site", sa.String(120), nullable=True),
        sa.Column("color", sa.String(7), nullable=True),
        sa.Column("logo_path", sa.String(120), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("loyalty_card")
