"""kitchen: recipes, meal plan, pantry

Revision ID: 6b8d0e2f4a71
Revises: 1f6c8e4b9d32
Create Date: 2026-11-03 16:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6b8d0e2f4a71"
down_revision: str | None = "1f6c8e4b9d32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("(CURRENT_TIMESTAMP)")


def upgrade() -> None:
    op.create_table(
        "recipe",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("ingredients", sa.Text(), nullable=True),
        sa.Column("steps", sa.Text(), nullable=True),
        sa.Column("link", sa.String(length=300), nullable=True),
        sa.Column("servings", sa.Integer(), nullable=True),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("tags", sa.String(length=120), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "meal_plan",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("meal", sa.String(length=12), nullable=False),
        sa.Column("recipe_id", sa.Integer(), nullable=True),
        sa.Column("text", sa.String(length=200), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipe.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meal_plan_day", "meal_plan", ["day"])
    op.create_table(
        "pantry_item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("qty", sa.String(length=30), nullable=True),
        sa.Column("place", sa.String(length=12), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("added_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["added_by_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pantry_item_expires_on", "pantry_item", ["expires_on"])


def downgrade() -> None:
    op.drop_index("ix_pantry_item_expires_on", table_name="pantry_item")
    op.drop_table("pantry_item")
    op.drop_index("ix_meal_plan_day", table_name="meal_plan")
    op.drop_table("meal_plan")
    op.drop_table("recipe")
