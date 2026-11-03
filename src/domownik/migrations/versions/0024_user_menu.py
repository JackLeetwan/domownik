"""user.menu: own bottom-bar shortcuts and hidden menu links

Revision ID: 8f2d6a4c0e93
Revises: 5e1b8d3f7c62
Create Date: 2026-11-03 19:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f2d6a4c0e93"
down_revision: str | None = "5e1b8d3f7c62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("menu", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("user", "menu")
