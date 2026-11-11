"""workout plans remember the catalog plan they were made from (new starter plans added later)

Revision ID: 9b4e2d7a1c63
Revises: 7a1d4e9b2c58
Create Date: 2026-11-11 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b4e2d7a1c63"
down_revision: str | None = "7a1d4e9b2c58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workout_plan") as batch:
        batch.add_column(sa.Column("seed", sa.String(30), nullable=True))
    # the starter plans made before (FBW 1–3, maybe renamed since – then they simply stay without a seed;
    # only plans marked `later` are ever added to a household that already has plans)
    conn = op.get_bind()
    for seed, name in (("fbw1", "FBW 1"), ("fbw2", "FBW 2"), ("fbw3", "FBW 3")):
        pid = conn.execute(
            sa.text("SELECT id FROM workout_plan WHERE name = :n ORDER BY id LIMIT 1"), {"n": name}
        ).scalar()
        if pid is not None:
            conn.execute(sa.text("UPDATE workout_plan SET seed = :s WHERE id = :i"), {"s": seed, "i": pid})


def downgrade() -> None:
    with op.batch_alter_table("workout_plan") as batch:
        batch.drop_column("seed")
