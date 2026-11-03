"""quick photos to describe later: attachment.inbox, attachment.capture_kind

Revision ID: 2e7c5a9b8d14
Revises: 9d4b6e1a3f57
Create Date: 2026-11-03 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2e7c5a9b8d14"
down_revision: str | None = "9d4b6e1a3f57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attachment") as batch:
        batch.add_column(sa.Column("inbox", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("capture_kind", sa.String(20), nullable=True))
        batch.create_index("ix_attachment_inbox", ["inbox"])


def downgrade() -> None:
    with op.batch_alter_table("attachment") as batch:
        batch.drop_index("ix_attachment_inbox")
        batch.drop_column("capture_kind")
        batch.drop_column("inbox")
