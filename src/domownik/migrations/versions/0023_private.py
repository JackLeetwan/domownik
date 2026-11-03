"""private_to_id on task, board_note, todo

Revision ID: 5e1b8d3f7c62
Revises: 2a7f4c9e6d15
Create Date: 2026-11-03 18:50:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5e1b8d3f7c62"
down_revision: str | None = "2a7f4c9e6d15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("task", "board_note", "todo")


def upgrade() -> None:
    for t in TABLES:
        # nullable column with an inline REFERENCES – SQLite adds it in place
        op.execute(f'ALTER TABLE "{t}" ADD COLUMN private_to_id INTEGER REFERENCES user (id) ON DELETE SET NULL')
        op.create_index(f"ix_{t}_private_to_id", t, ["private_to_id"])


def downgrade() -> None:
    for t in TABLES:
        op.drop_index(f"ix_{t}_private_to_id", table_name=t)
        with op.batch_alter_table(t, recreate="always") as batch:
            batch.drop_column("private_to_id")
