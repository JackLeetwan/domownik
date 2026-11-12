"""🩺 Zdrowie: medication, health_link; issue/attachment privacy; attachment/task.issue_id

Revision ID: 7c2e9a4b5d18
Revises: 4d8a1f6c3b27
Create Date: 2026-11-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c2e9a4b5d18"
down_revision: str | None = "4d8a1f6c3b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _private(table: str) -> sa.Column:
    return sa.Column(
        "private_to_id",
        sa.Integer(),
        sa.ForeignKey("user.id", name=f"fk_{table}_private_to_id", ondelete="SET NULL"),
        nullable=True,
    )


def _issue(table: str) -> sa.Column:
    return sa.Column(
        "issue_id",
        sa.Integer(),
        sa.ForeignKey("issue.id", name=f"fk_{table}_issue_id", ondelete="SET NULL"),
        nullable=True,
    )


def upgrade() -> None:
    with op.batch_alter_table("issue", recreate="always") as batch:
        batch.add_column(_private("issue"))
        batch.create_index("ix_issue_private_to_id", ["private_to_id"])
    with op.batch_alter_table("attachment", recreate="always") as batch:
        batch.add_column(_issue("attachment"))
        batch.add_column(_private("attachment"))
        batch.create_index("ix_attachment_issue_id", ["issue_id"])
        batch.create_index("ix_attachment_private_to_id", ["private_to_id"])
    with op.batch_alter_table("task", recreate="always") as batch:
        batch.add_column(_issue("task"))
        batch.create_index("ix_task_issue_id", ["issue_id"])
    op.create_table(
        "medication",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("item.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issue_id", sa.Integer(), sa.ForeignKey("issue.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("dose", sa.String(80), nullable=True),
        sa.Column("schedule", sa.String(120), nullable=True),
        sa.Column("started_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("prescription", sa.String(200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("private_to_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    for col in ("item_id", "issue_id", "private_to_id"):
        op.create_index(f"ix_medication_{col}", "medication", [col])
    op.create_table(
        "health_link",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("item.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issue_id", sa.Integer(), sa.ForeignKey("issue.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("private_to_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    for col in ("item_id", "issue_id", "private_to_id"):
        op.create_index(f"ix_health_link_{col}", "health_link", [col])


def downgrade() -> None:
    op.drop_table("health_link")
    op.drop_table("medication")
    with op.batch_alter_table("task", recreate="always") as batch:
        batch.drop_index("ix_task_issue_id")
        batch.drop_column("issue_id")
    with op.batch_alter_table("attachment", recreate="always") as batch:
        batch.drop_index("ix_attachment_private_to_id")
        batch.drop_index("ix_attachment_issue_id")
        batch.drop_column("private_to_id")
        batch.drop_column("issue_id")
    with op.batch_alter_table("issue", recreate="always") as batch:
        batch.drop_index("ix_issue_private_to_id")
        batch.drop_column("private_to_id")
