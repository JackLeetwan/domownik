"""stretching: the library of stretches and saved routines (🤸 Rozciąganie)

Revision ID: 9b4d2f7e1a38
Revises: 7a3c1e9d4b26
Create Date: 2026-11-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b4d2f7e1a38"
down_revision: str | None = "7a3c1e9d4b26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stretch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("area", sa.String(20), nullable=False),
        sa.Column("sided", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("seconds", sa.Integer(), server_default="30", nullable=False),
    )
    op.create_table(
        "stretch_routine",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("kind", sa.String(10), server_default="own", nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "stretch_routine_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("routine_id", sa.Integer(), sa.ForeignKey("stretch_routine.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stretch_id", sa.Integer(), sa.ForeignKey("stretch.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=True),
    )
    op.create_index("ix_stretch_routine_item_routine_id", "stretch_routine_item", ["routine_id"])


def downgrade() -> None:
    op.drop_index("ix_stretch_routine_item_routine_id", "stretch_routine_item")
    op.drop_table("stretch_routine_item")
    op.drop_table("stretch_routine")
    op.drop_table("stretch")
