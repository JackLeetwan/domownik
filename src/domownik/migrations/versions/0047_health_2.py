"""🩺 Zdrowie, stage 2: medicine reminders and prescription renewal, health readings

Revision ID: 3f8b1d6e9a72
Revises: 7c2e9a4b5d18
Create Date: 2026-11-12 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f8b1d6e9a72"
down_revision: str | None = "7c2e9a4b5d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("medication", recreate="always") as batch:
        batch.add_column(sa.Column("rx_until", sa.Date(), nullable=True))
        batch.add_column(
            sa.Column(
                "rx_task_id",
                sa.Integer(),
                sa.ForeignKey("task.id", name="fk_medication_rx_task_id", ondelete="SET NULL"),
                nullable=True,
            )
        )
    with op.batch_alter_table("supplement", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "medication_id",
                sa.Integer(),
                sa.ForeignKey("medication.id", name="fk_supplement_medication_id", ondelete="SET NULL"),
                nullable=True,
            )
        )
    op.create_table(
        "health_reading",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("item.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("value2", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("measured_on", sa.Date(), nullable=False),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("private_to_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_health_reading_item_id", "health_reading", ["item_id"])
    op.create_index("ix_health_reading_private_to_id", "health_reading", ["private_to_id"])


def downgrade() -> None:
    op.drop_table("health_reading")
    with op.batch_alter_table("supplement", recreate="always") as batch:
        batch.drop_column("medication_id")
    with op.batch_alter_table("medication", recreate="always") as batch:
        batch.drop_column("rx_task_id")
        batch.drop_column("rx_until")
