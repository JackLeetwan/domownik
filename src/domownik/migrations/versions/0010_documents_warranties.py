"""documents and photos with items, warranties / contracts with an end date

Revision ID: 51c48ad82469
Revises: 5c2e7a91d0b4
Create Date: 2026-11-03 09:52:22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "51c48ad82469"
down_revision: str | None = "5c2e7a91d0b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New tables only – no existing table is rebuilt, nothing that references them is touched.
    op.create_table(
        "attachment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("completion_id", sa.Integer(), nullable=True),
        sa.Column("storage_path", sa.String(length=120), nullable=False),
        sa.Column("original_name", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("doc_kind", sa.String(length=20), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("has_thumb", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["completion_id"], ["completion.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_path"),
    )
    op.create_index("ix_attachment_completion_id", "attachment", ["completion_id"], unique=False)
    op.create_index("ix_attachment_item_id", "attachment", ["item_id"], unique=False)
    op.create_table(
        "warranty",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("attachment_id", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachment.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_warranty_item_id", "warranty", ["item_id"], unique=False)


def downgrade() -> None:
    # Reminder tasks of warranties stay as ordinary one-off tasks; the files stay in data/uploads/files.
    op.drop_index("ix_warranty_item_id", table_name="warranty")
    op.drop_table("warranty")
    op.drop_index("ix_attachment_item_id", table_name="attachment")
    op.drop_index("ix_attachment_completion_id", table_name="attachment")
    op.drop_table("attachment")
