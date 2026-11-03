"""points & rewards: task/chore points, reward, point_entry

Revision ID: 9c3e5a7d1b84
Revises: 6b8d0e2f4a71
Create Date: 2026-11-03 17:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c3e5a7d1b84"
down_revision: str | None = "6b8d0e2f4a71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("(CURRENT_TIMESTAMP)")


def upgrade() -> None:
    for table in ("task", "chore"):
        op.add_column(table, sa.Column("points", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column("needs_approval", sa.Boolean(), server_default="0", nullable=False))
    op.create_table(
        "reward",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("icon", sa.String(length=16), nullable=True),
        sa.Column("cost", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "point_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("completion_id", sa.Integer(), nullable=True),
        sa.Column("chore_done_id", sa.Integer(), nullable=True),
        sa.Column("reward_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["completion_id"], ["completion.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chore_done_id"], ["chore_done.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reward_id"], ["reward.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_point_entry_user_id", "point_entry", ["user_id"])
    op.create_index("ix_point_entry_completion_id", "point_entry", ["completion_id"])
    op.create_index("ix_point_entry_chore_done_id", "point_entry", ["chore_done_id"])


def downgrade() -> None:
    op.drop_table("point_entry")
    op.drop_table("reward")
    for table in ("chore", "task"):
        op.drop_column(table, "needs_approval")
        op.drop_column(table, "points")
