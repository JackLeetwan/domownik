"""contacts (tradespeople, services, doctors) linked to one or more items

Revision ID: 7c1e5a9d2b40
Revises: 3deccfd9be0c
Create Date: 2026-11-03 10:28:42
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c1e5a9d2b40"
down_revision: str | None = "3deccfd9be0c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New tables only – no existing table is rebuilt. (Autogenerate also offers to re-create the unnamed
    # FKs of item.user_id / transaction.income_id – older ondelete drift, left alone on purpose.)
    op.create_table(
        "contact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("company", sa.String(length=120), nullable=True),
        sa.Column("role", sa.String(length=60), nullable=True),
        sa.Column("phones", sa.Text(), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("website", sa.String(length=300), nullable=True),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "contact_item",
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contact.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("contact_id", "item_id"),
    )
    op.create_index("ix_contact_item_item_id", "contact_item", ["item_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_contact_item_item_id", table_name="contact_item")
    op.drop_table("contact_item")
    op.drop_table("contact")
