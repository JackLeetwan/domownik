"""standing payments: "not one" / "confirmed over" marks (🔁 Stałe płatności)

Revision ID: 7a3c1e9d4b26
Revises: 5d2f8a6c3e17
Create Date: 2026-11-10 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a3c1e9d4b26"
down_revision: str | None = "5d2f8a6c3e17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "standing_mark",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("last_on", sa.Date(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("standing_mark")
