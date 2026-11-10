"""body: weight, measurements with photos, goal and reminders ("Forma" → Sylwetka)

Revision ID: 8b4e2d7f1a56
Revises: 6d1f8b3a9c27
Create Date: 2026-11-10 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b4e2d7f1a56"
down_revision: str | None = "6d1f8b3a9c27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "body_weight",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kg", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "day"),
    )
    op.create_index("ix_body_weight_user_id", "body_weight", ["user_id"])
    op.create_table(
        "body_check",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("chest", sa.Float(), nullable=True),
        sa.Column("arm", sa.Float(), nullable=True),
        sa.Column("waist", sa.Float(), nullable=True),
        sa.Column("thigh", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "day"),
    )
    op.create_index("ix_body_check_user_id", "body_check", ["user_id"])
    op.create_table(
        "body_photo",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("check_id", sa.Integer(), sa.ForeignKey("body_check.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pose", sa.String(10), nullable=False),
        sa.Column("storage_path", sa.String(120), nullable=False, unique=True),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("has_thumb", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("check_id", "pose"),
    )
    op.create_index("ix_body_photo_check_id", "body_photo", ["check_id"])
    op.create_table(
        "body_prefs",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("goal_kg", sa.Float(), nullable=True),
        sa.Column("reminders", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("weigh_gap_days", sa.Integer(), server_default="3", nullable=False),
        sa.Column("check_every_days", sa.Integer(), server_default="14", nullable=False),
        sa.Column("weigh_pushed_on", sa.Date(), nullable=True),
        sa.Column("check_pushed_on", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("body_prefs")
    op.drop_index("ix_body_photo_check_id", "body_photo")
    op.drop_table("body_photo")
    op.drop_index("ix_body_check_user_id", "body_check")
    op.drop_table("body_check")
    op.drop_index("ix_body_weight_user_id", "body_weight")
    op.drop_table("body_weight")
