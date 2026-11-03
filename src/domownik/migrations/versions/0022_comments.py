"""comments under tasks and notes

Revision ID: 2a7f4c9e6d15
Revises: 9c3e5a7d1b84
Create Date: 2026-11-03 18:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2a7f4c9e6d15"
down_revision: str | None = "9c3e5a7d1b84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("note_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["note_id"], ["board_note.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_comment_task_id", "comment", ["task_id"])
    op.create_index("ix_comment_note_id", "comment", ["note_id"])


def downgrade() -> None:
    op.drop_table("comment")
