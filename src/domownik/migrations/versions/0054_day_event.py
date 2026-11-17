"""day_event: all-day entries of the family calendar (holidays at whose family, trips, school breaks)

Revision ID: 7d3a9e2b6c41
Revises: 5b7e1d9c3a20
Create Date: 2026-11-17 22:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d3a9e2b6c41"
down_revision: str | None = "5b7e1d9c3a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "day_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("icon", sa.String(8), nullable=False, server_default="📌"),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("yearly", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("notify", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_day_event_starts_on", "day_event", ["starts_on"])


def downgrade() -> None:
    op.drop_index("ix_day_event_starts_on", "day_event")
    op.drop_table("day_event")
