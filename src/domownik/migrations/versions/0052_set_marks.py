"""🔥 marks on a logged set: warm-up, to failure, drop set (the lighter rounds right after it)

Revision ID: 9c4d2e7a1f53
Revises: 3e8a6c2f9b14
Create Date: 2026-11-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c4d2e7a1f53"
down_revision: str | None = "3e8a6c2f9b14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workout_set") as b:
        b.add_column(sa.Column("warmup", sa.Boolean(), server_default=sa.false(), nullable=False))
        b.add_column(sa.Column("failure", sa.Boolean(), server_default=sa.false(), nullable=False))
        b.add_column(sa.Column("drops", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workout_set") as b:
        b.drop_column("drops")
        b.drop_column("failure")
        b.drop_column("warmup")
