"""notes: title, checklist items, colour, label

Revision ID: b5e7c2d9a614
Revises: 7d2f9b3a1c48
Create Date: 2026-11-03 14:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e7c2d9a614"
down_revision: str | None = "7d2f9b3a1c48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("title", "items", "color", "label")


def upgrade() -> None:
    op.add_column("board_note", sa.Column("title", sa.String(length=120), nullable=True))
    op.add_column("board_note", sa.Column("items", sa.JSON(), nullable=True))
    op.add_column("board_note", sa.Column("color", sa.String(length=12), nullable=True))
    op.add_column("board_note", sa.Column("label", sa.String(length=40), nullable=True))


def downgrade() -> None:
    for c in reversed(COLUMNS):
        op.drop_column("board_note", c)
