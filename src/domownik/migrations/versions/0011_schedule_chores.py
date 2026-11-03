"""week schedule (fixed weekly slots, called-off occurrences), house chores with rotation

Revision ID: 3deccfd9be0c
Revises: 51c48ad82469
Create Date: 2026-11-03 10:04:09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3deccfd9be0c"
down_revision: str | None = "51c48ad82469"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New tables only – no existing table is rebuilt, nothing that references them is touched.
    # (Autogenerate also offered to re-create the unnamed FKs of item.user_id / transaction.income_id –
    # older drift in their ondelete clause, left alone on purpose: it would rebuild those tables.)
    op.create_table(
        "schedule_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("weekdays", sa.String(length=20), nullable=False),
        sa.Column("starts_at", sa.String(length=5), nullable=False),
        sa.Column("ends_at", sa.String(length=5), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("assign_mode", sa.String(length=10), server_default="fixed", nullable=False),
        sa.Column("assignee_id", sa.Integer(), nullable=True),
        sa.Column("alt_assignee_id", sa.Integer(), nullable=True),
        sa.Column("day_assignees", sa.JSON(), nullable=False),
        sa.Column("rota_anchor", sa.Date(), nullable=True),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("notify", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["alt_assignee_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assignee_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "schedule_skip",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("on_date", sa.Date(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["event_id"], ["schedule_event.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "on_date"),
    )
    op.create_index("ix_schedule_skip_event_id", "schedule_skip", ["event_id"], unique=False)
    op.create_table(
        "chore",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("room", sa.String(length=60), nullable=True),
        sa.Column("every_days", sa.Integer(), nullable=False),
        sa.Column("est_minutes", sa.Integer(), nullable=True),
        sa.Column("rotation", sa.JSON(), nullable=False),
        sa.Column("turn_user_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["turn_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "chore_done",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chore_id", sa.Integer(), nullable=False),
        sa.Column("done_on", sa.Date(), nullable=False),
        sa.Column("done_by_id", sa.Integer(), nullable=True),
        sa.Column("turn_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["chore_id"], ["chore.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["done_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["turn_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chore_done_chore_id", "chore_done", ["chore_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chore_done_chore_id", table_name="chore_done")
    op.drop_table("chore_done")
    op.drop_table("chore")
    op.drop_index("ix_schedule_skip_event_id", table_name="schedule_skip")
    op.drop_table("schedule_skip")
    op.drop_table("schedule_event")
