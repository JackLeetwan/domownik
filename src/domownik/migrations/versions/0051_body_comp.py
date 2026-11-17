"""🧬 body composition from a smart scale (Forma → Sylwetka)

Revision ID: 3e8a6c2f9b14
Revises: 7b3e5d1a9c42
Create Date: 2026-11-17 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e8a6c2f9b14"
down_revision: str | None = "7b3e5d1a9c42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VALUES = [
    "fat_pct",
    "muscle_kg",
    "water_pct",
    "protein_pct",
    "bone_kg",
    "visceral",
    "bmr_kcal",
    "bmi",
    "body_age",
    "score",
]


def upgrade() -> None:
    op.create_table(
        "body_comp",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        *[sa.Column(name, sa.Float(), nullable=True) for name in VALUES],
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "day"),
    )
    op.create_index("ix_body_comp_user_id", "body_comp", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_body_comp_user_id", "body_comp")
    op.drop_table("body_comp")
