"""task checklist: task_step

Revision ID: 7d2f9b3a1c48
Revises: 4c1e8a2f6b07
Create Date: 2026-11-03 13:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d2f9b3a1c48"
down_revision: str | None = "4c1e8a2f6b07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_step",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("done", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_step_task_id", "task_step", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_step_task_id", table_name="task_step")
    op.drop_table("task_step")
