"""stretch routines remember the catalog routine they were made from (new defaults added later)

Revision ID: 7a1d4e9b2c58
Revises: 5c2f8a7d3e41
Create Date: 2026-11-11 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a1d4e9b2c58"
down_revision: str | None = "5c2f8a7d3e41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("stretch_routine") as batch:
        batch.add_column(sa.Column("seed", sa.String(30), nullable=True))
    # the two defaults made before: "Przed treningiem" / "Po treningu" (renamed → the first of their kind)
    conn = op.get_bind()
    for seed, kind, name in (("before", "before", "Przed treningiem"), ("after", "after", "Po treningu")):
        rid = conn.execute(
            sa.text("SELECT id FROM stretch_routine WHERE name = :n ORDER BY id LIMIT 1"), {"n": name}
        ).scalar()
        if rid is None:
            rid = conn.execute(
                sa.text("SELECT id FROM stretch_routine WHERE kind = :k AND seed IS NULL ORDER BY id LIMIT 1"),
                {"k": kind},
            ).scalar()
        if rid is not None:
            conn.execute(sa.text("UPDATE stretch_routine SET seed = :s WHERE id = :i"), {"s": seed, "i": rid})


def downgrade() -> None:
    with op.batch_alter_table("stretch_routine") as batch:
        batch.drop_column("seed")
