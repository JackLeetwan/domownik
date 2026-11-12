"""📏 Wymiary: body measurements for buying clothes (body_size) and sizes that fit per brand (fit_size)

Revision ID: 4c7e9a2b5d18
Revises: 9d4a2f7c1e63
Create Date: 2026-11-12 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c7e9a2b5d18"
down_revision: str | None = "9d4a2f7c1e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FIELDS = (
    "height", "chest", "waist", "hips", "shoulders", "neck", "sleeve", "inseam",
    "head", "hand", "wrist", "foot_len", "foot_width",
)  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "body_size",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("item.id", ondelete="CASCADE"), nullable=False),
        sa.Column("measured_on", sa.Date(), nullable=False),
        *(sa.Column(f, sa.Float(), nullable=True) for f in FIELDS),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("private_to_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_body_size_item_id", "body_size", ["item_id"])
    op.create_index("ix_body_size_private_to_id", "body_size", ["private_to_id"])
    op.create_table(
        "fit_size",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("item.id", ondelete="CASCADE"), nullable=False),
        sa.Column("what", sa.String(40), nullable=False),
        sa.Column("brand", sa.String(60), nullable=True),
        sa.Column("size", sa.String(30), nullable=False),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_fit_size_item_id", "fit_size", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_fit_size_item_id", "fit_size")
    op.drop_table("fit_size")
    op.drop_index("ix_body_size_private_to_id", "body_size")
    op.drop_index("ix_body_size_item_id", "body_size")
    op.drop_table("body_size")
