"""📏 Wymiary: arm and leg girths (biceps, forearm, thigh, calf) and the outer leg length

Revision ID: 3c9f1a7e5b24
Revises: 5b8e2c4d7a19
Create Date: 2026-11-26 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c9f1a7e5b24"
down_revision: str | None = "5b8e2c4d7a19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("biceps", "forearm", "thigh", "calf", "outseam")


def upgrade() -> None:
    with op.batch_alter_table("body_size") as t:
        for c in COLUMNS:
            t.add_column(sa.Column(c, sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("body_size") as t:
        for c in COLUMNS:
            t.drop_column(c)
