"""task hour: due_time, ends_at, remind_minutes

Revision ID: 4c1e8a2f6b07
Revises: 9a4d3c7b5e21
Create Date: 2026-11-03 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c1e8a2f6b07"
down_revision: str | None = "9a4d3c7b5e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task", sa.Column("due_time", sa.String(length=5), nullable=True))
    op.add_column("task", sa.Column("ends_at", sa.String(length=5), nullable=True))
    op.add_column("task", sa.Column("remind_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("task", "remind_minutes")
    op.drop_column("task", "ends_at")
    op.drop_column("task", "due_time")
