"""stretching sessions: the history of the timer (🤸 Rozciąganie)

Revision ID: 3e8a5c1f9d62
Revises: 9b4d2f7e1a38
Create Date: 2026-11-11 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e8a5c1f9d62"
down_revision: str | None = "9b4d2f7e1a38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stretch_session",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("routine_id", sa.Integer(), sa.ForeignKey("stretch_routine.id", ondelete="SET NULL"), nullable=True),
        sa.Column("workout_id", sa.Integer(), sa.ForeignKey("workout.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("done", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("seconds", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index("ix_stretch_session_user_id", "stretch_session", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_stretch_session_user_id", "stretch_session")
    op.drop_table("stretch_session")
