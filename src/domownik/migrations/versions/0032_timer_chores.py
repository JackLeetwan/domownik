"""timers for chores ("Pranie" with ⏱ buttons) and the next step offered after a timer

Revision ID: 4d8a2e6f1c35
Revises: 7c3f1a9e2b60
Create Date: 2026-11-08 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4d8a2e6f1c35"
down_revision: str | None = "7c3f1a9e2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("timer") as batch:
        batch.add_column(sa.Column("chore_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("followup_dismissed", sa.Boolean(), nullable=False, server_default="0"))
        batch.create_foreign_key("fk_timer_chore_id_chore", "chore", ["chore_id"], ["id"], ondelete="SET NULL")
    with op.batch_alter_table("chore") as batch:
        batch.add_column(sa.Column("uses_timer", sa.Boolean(), nullable=False, server_default="0"))
    # a laundry chore that already exists gets the buttons right away
    op.execute("UPDATE chore SET uses_timer = 1 WHERE lower(name) LIKE 'pran%'")


def downgrade() -> None:
    with op.batch_alter_table("chore") as batch:
        batch.drop_column("uses_timer")
    with op.batch_alter_table("timer") as batch:
        batch.drop_constraint("fk_timer_chore_id_chore", type_="foreignkey")
        batch.drop_column("followup_dismissed")
        batch.drop_column("chore_id")
