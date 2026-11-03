"""attachment.task_id: files added in a task's form

Revision ID: e3a9f1c7b250
Revises: b5e7c2d9a614
Create Date: 2026-11-03 15:20:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3a9f1c7b250"
down_revision: str | None = "b5e7c2d9a614"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN with an inline REFERENCES – SQLite allows it for a nullable column, no table rebuild
    op.execute("ALTER TABLE attachment ADD COLUMN task_id INTEGER REFERENCES task (id) ON DELETE SET NULL")
    op.create_index("ix_attachment_task_id", "attachment", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_attachment_task_id", table_name="attachment")
    # SQLite cannot DROP a column that takes part in a foreign key – rebuild the table instead
    with op.batch_alter_table("attachment", recreate="always") as batch:
        batch.drop_column("task_id")
