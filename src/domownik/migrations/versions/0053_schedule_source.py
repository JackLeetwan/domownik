"""schedule_event.source: entries kept in step with an outside system (Zdrofit classes) through /api/grafik

Revision ID: 5b7e1d9c3a20
Revises: 9c4d2e7a1f53
Create Date: 2026-11-17 19:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b7e1d9c3a20"
down_revision: str | None = "9c4d2e7a1f53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedule_event") as b:
        b.add_column(sa.Column("source", sa.String(80), nullable=True))
        b.create_index("ix_schedule_event_source", ["source"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("schedule_event") as b:
        b.drop_index("ix_schedule_event_source")
        b.drop_column("source")
