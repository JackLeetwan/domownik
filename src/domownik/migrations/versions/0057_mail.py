"""✉️ Z poczty: emails from watched senders (the kindergarten) and the state of the mailbox check

Revision ID: 8e4b2d6f1a93
Revises: 3c9f1a7e5b24
Create Date: 2026-11-27 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8e4b2d6f1a93"
down_revision: str | None = "3c9f1a7e5b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_message",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.String(500), nullable=False, unique=True),
        sa.Column("sender", sa.String(200), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False, server_default=""),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False, server_default="new"),
        sa.Column("done_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("done_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mail_message_sent_at", "mail_message", ["sent_at"])
    op.create_index("ix_mail_message_status", "mail_message", ["status"])
    op.create_table(
        "mail_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("error_pushed_on", sa.Date(), nullable=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("item.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("mail_state")
    op.drop_index("ix_mail_message_status", "mail_message")
    op.drop_index("ix_mail_message_sent_at", "mail_message")
    op.drop_table("mail_message")
