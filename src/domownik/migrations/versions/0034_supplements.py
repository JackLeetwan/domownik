"""supplements: daily ✓ with a push at the hour, stock and the shopping list ("Forma")

Revision ID: 2a6c9e4f7b13
Revises: 9e3b5c7a1d42
Create Date: 2026-11-10 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2a6c9e4f7b13"
down_revision: str | None = "9e3b5c7a1d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supplement",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("dose", sa.String(40), nullable=True),
        sa.Column("time", sa.String(5), nullable=True),
        sa.Column("per_take", sa.Float(), server_default="1", nullable=False),
        sa.Column("pack_size", sa.Float(), nullable=True),
        sa.Column("stock", sa.Float(), nullable=True),
        sa.Column("restock_days", sa.Integer(), server_default="10", nullable=False),
        sa.Column("restock_listed", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("pushed_on", sa.Date(), nullable=True),
        sa.Column("again_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_supplement_user_id", "supplement", ["user_id"])
    op.create_table(
        "supplement_dose",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplement_id", sa.Integer(), sa.ForeignKey("supplement.id", ondelete="CASCADE"), nullable=False),
        sa.Column("taken_on", sa.Date(), nullable=False),
        sa.Column("taken_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("supplement_id", "taken_on"),
    )
    op.create_index("ix_supplement_dose_supplement_id", "supplement_dose", ["supplement_id"])


def downgrade() -> None:
    op.drop_index("ix_supplement_dose_supplement_id", "supplement_dose")
    op.drop_table("supplement_dose")
    op.drop_index("ix_supplement_user_id", "supplement")
    op.drop_table("supplement")
