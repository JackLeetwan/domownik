"""photos of recipes: attachment.recipe_id

Revision ID: 4d8a1f6c3b27
Revises: 9b4e2d7a1c63
Create Date: 2026-11-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4d8a1f6c3b27"
down_revision: str | None = "9b4e2d7a1c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # a foreign key cannot be added in place in SQLite – the table is rebuilt (foreign keys are off in env.py)
    with op.batch_alter_table("attachment", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "recipe_id",
                sa.Integer(),
                sa.ForeignKey("recipe.id", name="fk_attachment_recipe_id", ondelete="CASCADE"),
                nullable=True,
            )
        )
        batch.create_index("ix_attachment_recipe_id", ["recipe_id"])


def downgrade() -> None:
    op.execute("DELETE FROM attachment WHERE recipe_id IS NOT NULL")
    with op.batch_alter_table("attachment", recreate="always") as batch:
        batch.drop_index("ix_attachment_recipe_id")
        batch.drop_column("recipe_id")
