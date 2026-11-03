"""utility meters of items (electricity, water, gas, heat) and their readings

Revision ID: 2f8b6d4e1a93
Revises: 7c1e5a9d2b40
Create Date: 2026-11-03 10:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f8b6d4e1a93"
down_revision: str | None = "7c1e5a9d2b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New tables only – measurement (car odometer → mileage forecasts) stays as it is.
    # (Autogenerate also offers to re-create the unnamed FKs of item.user_id / transaction.income_id –
    # older ondelete drift, left alone on purpose.)
    op.create_table(
        "meter",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("unit", sa.String(length=10), nullable=False),
        sa.Column("unit_price", sa.Double(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meter_item_id", "meter", ["item_id"], unique=False)
    op.create_table(
        "meter_reading",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("meter_id", sa.Integer(), nullable=False),
        sa.Column("read_on", sa.Date(), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("is_reset", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("attachment_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachment.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["meter_id"], ["meter.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meter_reading_meter_id", "meter_reading", ["meter_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_meter_reading_meter_id", table_name="meter_reading")
    op.drop_table("meter_reading")
    op.drop_index("ix_meter_item_id", table_name="meter")
    op.drop_table("meter")
