"""photos of notes: attachment.note_id, attachment.item_id nullable

Revision ID: 7c1f3a9d2e48
Revises: 5b8e2f4a7c13
Create Date: 2026-11-03 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c1f3a9d2e48"
down_revision: str | None = "5b8e2f4a7c13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL cannot be dropped in place in SQLite – the table is rebuilt (foreign keys are off in env.py)
    with op.batch_alter_table("attachment", recreate="always") as batch:
        batch.alter_column("item_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(
            sa.Column(
                "note_id",
                sa.Integer(),
                sa.ForeignKey("board_note.id", name="fk_attachment_note_id", ondelete="CASCADE"),
                nullable=True,
            )
        )
        batch.create_index("ix_attachment_note_id", ["note_id"])


def downgrade() -> None:
    op.execute("DELETE FROM attachment WHERE item_id IS NULL")
    with op.batch_alter_table("attachment", recreate="always") as batch:
        batch.drop_index("ix_attachment_note_id")
        batch.drop_column("note_id")
        batch.alter_column("item_id", existing_type=sa.Integer(), nullable=False)
