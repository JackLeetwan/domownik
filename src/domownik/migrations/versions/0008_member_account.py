"""member account link: item.user_id

Revision ID: d8618a23c9a7
Revises: 4b0b465923fa
Create Date: 2026-11-02 16:03:08.237828
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "d8618a23c9a7"
down_revision: str | None = "4b0b465923fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD COLUMN with an inline REFERENCES – never batch_alter_table("item") here: recreating
    # the table drops the old one, and with foreign_keys=ON that cascades into tasks, issues, children…
    op.execute('ALTER TABLE item ADD COLUMN user_id INTEGER REFERENCES "user" (id) ON DELETE SET NULL')

    # Link a household member to an account only when it is unambiguous: exactly one active
    # member item and exactly one active account carry the very same name. Nothing else is guessed.
    conn = op.get_bind()
    members = conn.execute(sa.text("SELECT id, name FROM item WHERE kind = 'member' AND archived = 0")).fetchall()
    users = conn.execute(sa.text("SELECT id, name FROM user WHERE is_active = 1")).fetchall()
    for item_id, name in members:
        same_items = [i for i, n in members if n.strip() == name.strip()]
        same_users = [u for u, n in users if n.strip() == name.strip()]
        if len(same_items) == 1 and len(same_users) == 1:
            conn.execute(sa.text("UPDATE item SET user_id = :u WHERE id = :i"), {"u": same_users[0], "i": item_id})


def downgrade() -> None:
    # SQLite cannot DROP a column that takes part in a foreign key, so the table is rebuilt – with
    # foreign keys off, or dropping the old table would cascade-delete every task and sub-item.
    conn = op.get_bind()
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("Cannot switch foreign keys off inside a transaction – refusing to rebuild 'item'")
    with op.batch_alter_table("item", schema=None) as batch_op:
        batch_op.drop_column("user_id")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
