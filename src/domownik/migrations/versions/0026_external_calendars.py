"""external calendars (ICS links) and their events

Revision ID: 5b8e2f4a7c13
Revises: 3d7a9c1e5b26
Create Date: 2026-11-03 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b8e2f4a7c13"
down_revision: str | None = "3d7a9c1e5b26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_calendar",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("icon", sa.String(8), server_default="📅", nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("private_to_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE")),
        sa.Column("notify", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("fetched_at", sa.DateTime()),
        sa.Column("error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_external_calendar_private_to_id", "external_calendar", ["private_to_id"])
    op.create_table(
        "external_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "calendar_id", sa.Integer(), sa.ForeignKey("external_calendar.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("starts_at", sa.String(5)),
        sa.Column("ends_at", sa.String(5)),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("location", sa.String(200)),
    )
    op.create_index("ix_external_event_calendar_id", "external_event", ["calendar_id"])
    op.create_index("ix_external_event_starts_on", "external_event", ["starts_on"])


def downgrade() -> None:
    op.drop_table("external_event")
    op.drop_table("external_calendar")
