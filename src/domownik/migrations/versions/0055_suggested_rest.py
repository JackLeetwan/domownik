"""⏱ The plans' rests by the kind of exercise instead of 90 s everywhere (training.suggested_rest, frozen here)

Only items still at the old default 90 s change – a rest set by hand stays; in a superset its members get the
longest of them (the rest comes after the round).

Revision ID: 5b8e2c4d7a19
Revises: 7d3a9e2b6c41
Create Date: 2026-11-18 23:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b8e2c4d7a19"
down_revision: str | None = "7d3a9e2b6c41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMPOUND = {"squat", "hinge", "lunge", "push_h", "push_v", "pull_h", "pull_v"}
SMALL = {"lateral", "rear_delt", "calves", "core_flex", "core_static"}


def _rest(key: str | None, pattern: str, equipment: str) -> int:
    if pattern == "mobility":
        return 30
    if pattern in COMPOUND or (pattern == "glute" and "thrust" in (key or "")):
        if equipment == "barbell":
            return 180 if pattern in ("squat", "hinge") else 150
        return 90 if equipment in ("bodyweight", "band") else 120
    return 60 if pattern in SMALL or pattern == "glute" else 90


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT i.id, i.plan_id, i.superset, i.rest_seconds, e.key, e.pattern, e.equipment"
            " FROM plan_item i JOIN exercise e ON e.id = i.exercise_id"
        )
    ).all()
    new = {r.id: _rest(r.key, r.pattern, r.equipment) for r in rows if r.rest_seconds == 90}
    groups: dict[tuple[int, int], list[int]] = {}
    for r in rows:
        if r.id in new and r.superset is not None:
            groups.setdefault((r.plan_id, r.superset), []).append(r.id)
    for ids in groups.values():
        top = max(new[i] for i in ids)
        for i in ids:
            new[i] = top
    for item_id, rest in new.items():
        if rest != 90:
            conn.execute(sa.text("UPDATE plan_item SET rest_seconds = :r WHERE id = :i"), {"r": rest, "i": item_id})


def downgrade() -> None:
    pass  # which items were at the default is not kept
