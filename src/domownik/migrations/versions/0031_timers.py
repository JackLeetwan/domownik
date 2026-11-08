"""timers: a countdown with a push when it ends ("Pranie – 2 h")

Revision ID: 7c3f1a9e2b60
Revises: 5b8e2d7c4a19
Create Date: 2026-11-08 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c3f1a9e2b60"
down_revision: str | None = "5b8e2d7c4a19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "timer",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("renotified_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_timer_ends_at", "timer", ["ends_at"])
    op.create_index("ix_timer_finished_at", "timer", ["finished_at"])


def downgrade() -> None:
    op.drop_index("ix_timer_finished_at", "timer")
    op.drop_index("ix_timer_ends_at", "timer")
    op.drop_table("timer")
