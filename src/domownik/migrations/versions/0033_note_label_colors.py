"""the colour of a whole note category ("Fit" – pink)

Revision ID: 9e3b5c7a1d42
Revises: 4d8a2e6f1c35
Create Date: 2026-11-09 09:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9e3b5c7a1d42"
down_revision: str | None = "4d8a2e6f1c35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "note_label_color",
        sa.Column("label", sa.String(length=40), primary_key=True),
        sa.Column("color", sa.String(length=12), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("note_label_color")
