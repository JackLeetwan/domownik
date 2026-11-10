"""training: the exercise library, plans, workouts with their sets, the weekly goal ("Forma")

Revision ID: 6d1f8b3a9c27
Revises: 2a6c9e4f7b13
Create Date: 2026-11-10 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6d1f8b3a9c27"
down_revision: str | None = "2a6c9e4f7b13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exercise",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(40), nullable=True, unique=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("en", sa.String(400), nullable=True),
        sa.Column("pattern", sa.String(20), nullable=False),
        sa.Column("muscle", sa.String(20), nullable=False),
        sa.Column("equipment", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(10), server_default="reps", nullable=False),
        sa.Column("step", sa.Float(), server_default="2.5", nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "workout_plan",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "plan_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("workout_plan.id", ondelete="CASCADE"), nullable=False),
        sa.Column("exercise_id", sa.Integer(), sa.ForeignKey("exercise.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("sets", sa.Integer(), nullable=False),
        sa.Column("reps_min", sa.Integer(), nullable=True),
        sa.Column("reps_max", sa.Integer(), nullable=True),
        sa.Column("seconds", sa.Integer(), nullable=True),
        sa.Column("rest_seconds", sa.Integer(), nullable=False),
        sa.Column("superset", sa.Integer(), nullable=True),
    )
    op.create_index("ix_plan_item_plan_id", "plan_item", ["plan_id"])
    op.create_table(
        "workout",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("workout_plan.id", ondelete="SET NULL"), nullable=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("equipment", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(10), server_default="app", nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("rest_until", sa.DateTime(), nullable=True),
        sa.Column("rest_pushed", sa.Boolean(), server_default="0", nullable=False),
    )
    op.create_index("ix_workout_user_id", "workout", ["user_id"])
    op.create_index("ix_workout_day", "workout", ["day"])
    op.create_index("ix_workout_rest_until", "workout", ["rest_until"])
    op.create_table(
        "workout_exercise",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workout_id", sa.Integer(), sa.ForeignKey("workout.id", ondelete="CASCADE"), nullable=False),
        sa.Column("exercise_id", sa.Integer(), sa.ForeignKey("exercise.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("sets", sa.Integer(), nullable=False),
        sa.Column("reps_min", sa.Integer(), nullable=True),
        sa.Column("reps_max", sa.Integer(), nullable=True),
        sa.Column("seconds", sa.Integer(), nullable=True),
        sa.Column("rest_seconds", sa.Integer(), nullable=False),
        sa.Column("superset", sa.Integer(), nullable=True),
        sa.Column("swapped_from_id", sa.Integer(), sa.ForeignKey("exercise.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_workout_exercise_workout_id", "workout_exercise", ["workout_id"])
    op.create_index("ix_workout_exercise_exercise_id", "workout_exercise", ["exercise_id"])
    op.create_table(
        "workout_set",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workout_exercise_id",
            sa.Integer(),
            sa.ForeignKey("workout_exercise.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("set_no", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("reps", sa.Integer(), nullable=True),
        sa.Column("seconds", sa.Integer(), nullable=True),
        sa.Column("done_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_workout_set_workout_exercise_id", "workout_set", ["workout_exercise_id"])
    op.create_table(
        "training_prefs",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("weekly_goal", sa.Integer(), server_default="3", nullable=False),
        sa.Column("gap_days", sa.Integer(), server_default="3", nullable=False),
        sa.Column("gap_pushed_on", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("training_prefs")
    op.drop_index("ix_workout_set_workout_exercise_id", "workout_set")
    op.drop_table("workout_set")
    op.drop_index("ix_workout_exercise_exercise_id", "workout_exercise")
    op.drop_index("ix_workout_exercise_workout_id", "workout_exercise")
    op.drop_table("workout_exercise")
    op.drop_index("ix_workout_rest_until", "workout")
    op.drop_index("ix_workout_day", "workout")
    op.drop_index("ix_workout_user_id", "workout")
    op.drop_table("workout")
    op.drop_index("ix_plan_item_plan_id", "plan_item")
    op.drop_table("plan_item")
    op.drop_table("workout_plan")
    op.drop_table("exercise")
