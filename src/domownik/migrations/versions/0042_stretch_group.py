"""stretching together: one shared timer (👫 razem)

Revision ID: 5c2f8a7d3e41
Revises: 3e8a5c1f9d62
Create Date: 2026-11-11 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c2f8a7d3e41"
down_revision: str | None = "3e8a5c1f9d62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stretch_group",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invited", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("rev", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    with op.batch_alter_table("stretch_session", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "group_id",
                sa.Integer(),
                sa.ForeignKey("stretch_group.id", name="fk_stretch_session_group_id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        batch.create_index("ix_stretch_session_group_id", ["group_id"])


def downgrade() -> None:
    with op.batch_alter_table("stretch_session", recreate="always") as batch:
        batch.drop_index("ix_stretch_session_group_id")
        batch.drop_column("group_id")
    op.drop_table("stretch_group")
