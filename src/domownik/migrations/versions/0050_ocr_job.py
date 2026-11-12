"""📝 Reading documents: the queue of photos / scans / PDFs read into text by the worker on the PC

Revision ID: 7b3e5d1a9c42
Revises: 4c7e9a2b5d18
Create Date: 2026-11-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7b3e5d1a9c42"
down_revision: str | None = "4c7e9a2b5d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ocr_job",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "attachment_id",
            sa.Integer(),
            sa.ForeignKey("attachment.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(10), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("source", sa.String(20), nullable=True),
        sa.Column("model", sa.String(80), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("done_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_ocr_job_status", "ocr_job", ["status"])


def downgrade() -> None:
    op.drop_index("ix_ocr_job_status", table_name="ocr_job")
    op.drop_table("ocr_job")
