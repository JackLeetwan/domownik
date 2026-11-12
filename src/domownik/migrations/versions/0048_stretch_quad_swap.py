"""🤸 "Przód uda w klęku podpartym" (on one hand and a knee) out of the routines → "Przód uda leżąc na boku"

Revision ID: 9d4a2f7c1e63
Revises: 3f8b1d6e9a72
Create Date: 2026-11-12 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d4a2f7c1e63"
down_revision: str | None = "3f8b1d6e9a72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id(conn, key: str) -> int | None:
    return conn.execute(sa.text("SELECT id FROM stretch WHERE key = :k"), {"k": key}).scalar()


def upgrade() -> None:
    conn = op.get_bind()
    old, new = _id(conn, "quad_all_fours"), _id(conn, "quad_side")
    if old is None or new is None:
        return  # a fresh database: the routines come from the catalog, already without it
    conn.execute(
        sa.text(
            "UPDATE stretch_routine_item SET stretch_id = :new, seconds = NULL WHERE stretch_id = :old"
            " AND routine_id IN (SELECT id FROM stretch_routine WHERE archived = 0)"
        ),
        {"old": old, "new": new},
    )


def downgrade() -> None:
    pass  # which items had it is not kept
