"""together: a group of workouts trained together, the shared slot of an exercise ("Forma" → 👫)

Revision ID: 3c7a9e1b5d84
Revises: 8b4e2d7f1a56
Create Date: 2026-11-10 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7a9e1b5d84"
down_revision: str | None = "8b4e2d7f1a56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workout_group",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invited", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    with op.batch_alter_table("workout") as b:
        b.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        b.create_foreign_key("fk_workout_group_id", "workout_group", ["group_id"], ["id"], ondelete="SET NULL")
        b.create_index("ix_workout_group_id", ["group_id"])
    with op.batch_alter_table("workout_exercise") as b:
        b.add_column(sa.Column("slot", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workout_exercise") as b:
        b.drop_column("slot")
    with op.batch_alter_table("workout") as b:
        b.drop_index("ix_workout_group_id")
        b.drop_constraint("fk_workout_group_id", type_="foreignkey")
        b.drop_column("group_id")
    op.drop_table("workout_group")
