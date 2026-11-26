"""Training ("Forma"): plans in rotation, sets logged on the phone, today's weight suggested, substitutes.

Private: a workout belongs to one person, only the exercise library and the plans are shared.

Progression – double progression: a plan item has a range (8–12). When every planned set of the last
session reached the top of the range with the same weight, today's suggestion is that weight + the
exercise's step and the bottom of the range; otherwise the same weight, aiming at one more rep.

Substitutes – the same movement pattern first (a busy bench → dumbbells, machine), then the same muscle.
A workout "na wyjeździe" keeps only equipment at hand (away list of the catalog) and swaps the plan's
exercises for such substitutes when it starts.

Rest – logging a set starts the rest (the item's rest_seconds; inside a superset only after its last
exercise). The page counts it down; the job every few seconds pushes when it ends (the phone in a pocket).
"""

from __future__ import annotations

import functools
import itertools
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from domownik import browse, webpush
from domownik.config import now
from domownik.models import (
    Exercise,
    PlanItem,
    TrainingPrefs,
    User,
    Workout,
    WorkoutExercise,
    WorkoutPlan,
    WorkoutSet,
)

log = logging.getLogger("domownik.training")

CATALOG = Path(__file__).parent / "catalog" / "seed" / "exercises.yaml"
STATIC = Path(__file__).parent / "static"
REST_PUSH_LATE = timedelta(minutes=2)  # a server back later does not push about an old rest
NUDGE_HOUR = 17  # "4 dni bez treningu" goes out at this hour
DEFAULT_REST = 90

# ⏱ Suggested rest between sets, for muscle mass in 8–20 reps. Longer than 60 s helps a little, beyond ~90 s
# the gain levels off (Singer, Wolf, Schoenfeld et al. 2024, Bayesian meta-analysis); heavy multi-joint
# lifts need more to keep the reps of the next set (Schoenfeld 2016: 3 min > 1 min) – so by the movement:
COMPOUND = {"squat", "hinge", "lunge", "push_h", "push_v", "pull_h", "pull_v"}
SMALL = {"lateral", "rear_delt", "calves", "core_flex", "core_static"}  # small muscles, short sets: 60 s


def suggested_rest(ex: Exercise) -> int:
    """Seconds of rest after a set of this exercise: heavy barbell squat / deadlift 3 min, other barbell
    compounds 2½, compounds with dumbbells, machines, cables or a bar 2, bodyweight / band 1½;
    isolation 1½ (small muscles and the core 1); a hip thrust counts as a compound, mobility ½."""
    if ex.pattern == "mobility":
        return 30
    if ex.pattern in COMPOUND or (ex.pattern == "glute" and "thrust" in (ex.key or "")):
        if ex.equipment == "barbell":
            return 180 if ex.pattern in ("squat", "hinge") else 150
        return 90 if ex.equipment in ("bodyweight", "band") else 120
    return 60 if ex.pattern in SMALL or ex.pattern == "glute" else 90


def superset_rests(items) -> None:
    """Inside a superset the rest comes after the round – as long as its most demanding exercise needs."""
    groups: dict[int, list] = {}
    for it in items:
        if it.superset is not None:
            groups.setdefault(it.superset, []).append(it)
    for members in groups.values():
        top = max(m.rest_seconds for m in members)
        for m in members:
            m.rest_seconds = top


def _now() -> datetime:
    return now().replace(tzinfo=None)


@functools.cache
def catalog() -> dict[str, Any]:
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))


def pattern_label(key: str) -> str:
    return catalog()["patterns"].get(key, key)


def muscle_label(key: str) -> str:
    return catalog()["muscles"].get(key, key)


def equipment_label(key: str) -> str:
    return catalog()["equipment"].get(key, key)


IMAGES = CATALOG.parent / "exercise_images.yaml"
# when there is no picture: what the exercise is done with
EQUIPMENT_ICON = {
    "barbell": "🏋️",
    "dumbbell": "💪",
    "machine": "⚙️",
    "cable": "🔗",
    "kettlebell": "🔔",
    "bar": "🤸",
    "bodyweight": "🧍",
    "band": "🎗️",
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@functools.cache
def _images() -> tuple[dict[str, str], dict[str, str]]:
    data = yaml.safe_load(IMAGES.read_text(encoding="utf-8"))
    by_key = {k: _slug(v) for k, v in (data.get("by_key") or {}).items() if v}
    by_name = {k.strip().lower(): _slug(v) for k, v in (data.get("by_name") or {}).items() if v}
    return by_key, by_name


def image(ex: Exercise) -> str | None:
    """The picture of the exercise (static/exercises/…webp) – by its catalog key, else by one of its names."""
    by_key, by_name = _images()
    slug = by_key.get(ex.key) if ex.key else None
    if slug is None:
        for alias in [ex.name, *(ex.en or "").split("|")]:
            slug = by_name.get(alias.strip().lower())
            if slug:
                break
    return f"/static/exercises/{slug}.webp" if slug else None


def image_end(ex: Exercise) -> str | None:
    """The second frame (the end position) – shown next to the first one with the instructions."""
    start = image(ex)
    if start is None:
        return None
    end = start.removesuffix(".webp") + "-1.webp"
    return end if (STATIC / end.removeprefix("/static/")).is_file() else None


GUIDES = CATALOG.parent / "exercise_guides.yaml"


@dataclass(frozen=True)
class Guide:
    """How an exercise is done: a few steps and the most common mistakes."""

    steps: list[str]
    mistakes: list[str]


@functools.cache
def _guides() -> tuple[dict[str, Guide], dict[str, Guide]]:
    data = yaml.safe_load(GUIDES.read_text(encoding="utf-8"))

    def load(section: str) -> dict[str, Guide]:
        return {
            k: Guide(steps=list(v.get("steps") or []), mistakes=list(v.get("mistakes") or []))
            for k, v in (data.get(section) or {}).items()
        }

    by_name = {k.strip().lower(): g for k, g in load("by_name").items()}
    return load("by_key"), by_name


@functools.cache
def stretch_guides() -> dict[str, Guide]:
    """The instructions of the stretches (stretching.py) – the `stretch` section, by the stretch's key."""
    data = yaml.safe_load(GUIDES.read_text(encoding="utf-8"))
    return {
        k: Guide(steps=list(v.get("steps") or []), mistakes=list(v.get("mistakes") or []))
        for k, v in (data.get("stretch") or {}).items()
    }


def guide(ex: Exercise) -> Guide | None:
    """The instructions of the exercise – by its catalog key, else by one of its names (a Jefit import)."""
    by_key, by_name = _guides()
    if ex.key and ex.key in by_key:
        return by_key[ex.key]
    for alias in [ex.name, *(ex.en or "").split("|")]:
        g = by_name.get(alias.strip().lower())
        if g:
            return g
    return None


def equipment_icon(ex: Exercise) -> str:
    return EQUIPMENT_ICON.get(ex.equipment, "🏋️")


AWAY = "away"


def away_equipment() -> list[str]:
    return list(catalog()["away"])


def range_for(target: int) -> tuple[int, int]:
    """The plan's "4×12" as a range for double progression: 12 → 8–12, 16 → 12–16, 20 → 15–20."""
    return max(1, target - max(4, round(target / 4))), target


# ---------------------------------------------------------------- library and plans


def _adopt_imported(db: Session) -> None:
    """An exercise the Jefit import made under its English name, now in the catalog (by that name in `en`):
    it becomes the catalog one – takes the key, or, when the catalog one exists already, its history moves there."""
    by_alias = {a.strip().lower(): row["key"] for row in catalog()["exercises"] for a in row.get("en") or []}
    have = {e.key: e for e in db.scalars(select(Exercise).where(Exercise.key.is_not(None)))}
    for ex in list(db.scalars(select(Exercise).where(Exercise.key.is_(None)))):
        key = next((by_alias[a] for a in (ex.name.lower(), *(ex.en or "").lower().split("|")) if a in by_alias), None)
        if key is None:
            continue
        target = have.get(key)
        if target is None:
            ex.key = key
            have[key] = ex
            continue
        db.execute(update(WorkoutExercise).where(WorkoutExercise.exercise_id == ex.id).values(exercise_id=target.id))
        db.execute(
            update(WorkoutExercise).where(WorkoutExercise.swapped_from_id == ex.id).values(swapped_from_id=target.id)
        )
        db.execute(update(PlanItem).where(PlanItem.exercise_id == ex.id).values(exercise_id=target.id))
        db.delete(ex)
    db.flush()


def ensure_library(db: Session) -> None:
    """Seeded exercises missing in the database are added; the existing ones follow the catalog (name, Jefit
    names, pattern, muscle, equipment, step) – the catalog is the one place they are named."""
    _adopt_imported(db)
    have = {e.key: e for e in db.scalars(select(Exercise).where(Exercise.key.is_not(None)))}
    for row in catalog()["exercises"]:
        en = "|".join(row.get("en") or [])
        ex = have.get(row["key"])
        if ex is None:
            db.add(
                Exercise(
                    key=row["key"],
                    name=row["name"],
                    en=en,
                    pattern=row["pattern"],
                    muscle=row["muscle"],
                    equipment=row["equipment"],
                    kind=row.get("kind", "reps"),
                    step=float(row.get("step", 2.5)),
                )
            )
        else:
            ex.en, ex.name = en, row["name"]
            ex.pattern, ex.muscle, ex.equipment = row["pattern"], row["muscle"], row["equipment"]
            ex.step = float(row.get("step", 2.5))
    db.flush()


def seed_plans(db: Session) -> list[WorkoutPlan]:
    """The starter plans – all of them into a household without any plan; into one that has plans only the
    ones added to the catalog later (`later: true`) and never made. A removed one stays archived, not remade."""
    has_plans = bool(db.scalar(select(func.count(WorkoutPlan.id))))
    made_from = set(db.scalars(select(WorkoutPlan.seed).where(WorkoutPlan.seed.is_not(None))))
    todo = [p for p in catalog()["plans"] if p["key"] not in made_from and (not has_plans or p.get("later"))]
    if not todo:
        return []
    ensure_library(db)
    by_key = {e.key: e for e in db.scalars(select(Exercise).where(Exercise.key.is_not(None)))}
    last = db.scalar(select(func.max(WorkoutPlan.position)))
    pos = 0 if last is None else last + 1
    out = []
    for p in todo:
        plan = WorkoutPlan(name=p["name"], position=pos, seed=p["key"])
        pos += 1
        for i, it in enumerate(p["items"]):
            lo, hi = range_for(it["reps"]) if it.get("reps") else (None, None)
            plan.items.append(
                PlanItem(
                    exercise_id=by_key[it["ex"]].id,
                    position=i,
                    sets=it.get("sets", 4),
                    reps_min=lo,
                    reps_max=hi,
                    seconds=it.get("seconds"),
                    rest_seconds=it.get("rest") or suggested_rest(by_key[it["ex"]]),
                    superset=it.get("superset"),
                )
            )
        superset_rests([i for i in plan.items if "rest" not in p["items"][i.position]])
        db.add(plan)
        out.append(plan)
    db.flush()
    return out


def plans(db: Session) -> list[WorkoutPlan]:
    return list(
        db.scalars(
            select(WorkoutPlan)
            .where(WorkoutPlan.archived.is_(False))
            .order_by(WorkoutPlan.position, WorkoutPlan.id)
            .options(selectinload(WorkoutPlan.items).selectinload(PlanItem.exercise))
        )
    )


def _series(n: int) -> str:
    return "seria" if n == 1 else "serie" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "serii"


def plan_entry(p: WorkoutPlan) -> browse.Entry:
    """The plan as a row of the list (browse.py, shared with the stretching routines): body groups by sets,
    ▶ Start, its exercises to search by; "🧳 Na wyjazd" – everything in it can be done away from the gym."""
    amounts: dict[str, float] = {}
    for it in p.items:
        g = browse.group_of("strength", it.exercise.muscle)
        if g:
            amounts[g] = amounts.get(g, 0) + it.sets
    sets = sum(it.sets for it in p.items)
    away = set(away_equipment())
    words = " ".join(
        f"{it.exercise.name} {muscle_label(it.exercise.muscle)} {equipment_label(it.exercise.equipment)}"
        for it in p.items
    )
    return browse.Entry(
        id=p.id,
        name=p.name,
        href=f"/forma/plany/{p.id}",
        meta=f"{len(p.items)} ćw. · {sets} {_series(sets)}",
        amounts=amounts,
        position=p.position,
        size=sets,
        start="/forma/trening" if p.items else None,
        start_fields={"plan_id": p.id},
        free=all(it.exercise.equipment in away for it in p.items),
        search=words,
    )


def recent_plans(db: Session, user: User) -> dict[int, int]:
    """Plan id → 0 for the one done most recently by this person, 1 for the one before…"""
    rank: dict[int, int] = {}
    for pid in db.scalars(
        select(Workout.plan_id)
        .where(Workout.user_id == user.id, Workout.plan_id.is_not(None))
        .order_by(Workout.day.desc(), Workout.started_at.desc())
        .limit(50)
    ):
        rank.setdefault(pid, len(rank))
    return rank


def exercises(db: Session) -> list[Exercise]:
    return list(db.scalars(select(Exercise).order_by(Exercise.name)))


# ---------------------------------------------------------------- workouts


def mine(db: Session, user: User, limit: int | None = None) -> list[Workout]:
    stmt = (
        select(Workout)
        .where(Workout.user_id == user.id)
        .order_by(Workout.day.desc(), Workout.started_at.desc())
        .options(selectinload(Workout.exercises).selectinload(WorkoutExercise.done), selectinload(Workout.plan))
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def get_own(db: Session, user: User, workout_id: int) -> Workout | None:
    w = db.get(Workout, workout_id)
    return w if w is not None and w.user_id == user.id else None


def drop_stale_empty(db: Session, user: User, today: date) -> int:
    """Workouts left started on an earlier day with not one set – an abandoned start, nothing to keep."""
    stale = [
        w
        for w in db.scalars(
            select(Workout).where(Workout.user_id == user.id, Workout.finished_at.is_(None), Workout.day < today)
        )
        if not counts(w)
    ]
    for w in stale:
        db.delete(w)
    db.flush()
    return len(stale)


def active(db: Session, user: User) -> Workout | None:
    """The workout still going (started, not finished) – the newest one."""
    return db.scalar(
        select(Workout)
        .where(Workout.user_id == user.id, Workout.finished_at.is_(None))
        .order_by(Workout.started_at.desc())
        .limit(1)
    )


@dataclass
class Bubble:
    """🏋️ a workout going on today, for the bubble on every other page: where to go back, the rest's end."""

    workout_id: int
    rest_ms: int | None  # epoch ms of the rest's end (None: no rest running)


def bubble(db: Session, user_id: int, today: date) -> Bubble | None:
    w = db.scalar(
        select(Workout)
        .where(Workout.user_id == user_id, Workout.finished_at.is_(None), Workout.day == today)
        .order_by(Workout.started_at.desc())
        .limit(1)
    )
    if w is None:
        return None
    rest_ms = None
    stale = _now() - timedelta(minutes=10)
    if w.rest_until is not None and w.rest_until > stale:
        tz = now().tzinfo
        rest_ms = int(w.rest_until.replace(tzinfo=tz).timestamp() * 1000)
    return Bubble(w.id, rest_ms)


SAVED_SEED = "z-treningu"  # a plan saved from a workout made up on the spot: started on purpose, not in the cycle


def _off_rotation() -> set[str]:
    """Catalog plans done on occasion (`rotation: false` – the one for two), not in the FBW 1 → 2 → 3 cycle;
    nor plans saved from a workout."""
    return {p["key"] for p in catalog()["plans"] if p.get("rotation") is False} | {SAVED_SEED}


def next_plan(db: Session, user: User) -> WorkoutPlan | None:
    """The plan after the one done last (FBW 1 → 2 → 3 → 1); the first one before any. A plan out of the
    rotation is neither suggested nor counted as the last one – it doesn't break the cycle."""
    off = _off_rotation()
    ps = [p for p in plans(db) if p.seed not in off] or plans(db)
    if not ps:
        return None
    ids = [p.id for p in ps]
    last = db.scalar(
        select(Workout.plan_id)
        .where(Workout.user_id == user.id, Workout.plan_id.in_(ids))
        .order_by(Workout.day.desc(), Workout.started_at.desc())
        .limit(1)
    )
    if last not in ids:
        return ps[0]
    return ps[(ids.index(last) + 1) % len(ps)]


def allowed(w: Workout) -> set[str] | None:
    return set(w.equipment) if w.equipment else None


EQUIPMENT_ORDER = ("barbell", "dumbbell", "machine", "cable", "kettlebell", "bar", "bodyweight", "band")


def substitutes(db: Session, ex: Exercise, allowed_eq: set[str] | None = None) -> tuple[list[Exercise], list[Exercise]]:
    """(the same pattern, the same muscle only) – other exercises, only with allowed equipment.

    At the gym free weights and machines first; on the road (allowed_eq) the order of the away list."""
    order = list(EQUIPMENT_ORDER) if allowed_eq is None else [e for e in away_equipment() if e in allowed_eq]

    def rank(e: Exercise) -> tuple[int, str]:
        return (order.index(e.equipment) if e.equipment in order else 99, e.name)

    same_pattern, same_muscle = [], []
    for other in exercises(db):
        if other.id == ex.id or (allowed_eq is not None and other.equipment not in allowed_eq):
            continue
        if other.pattern == ex.pattern:
            same_pattern.append(other)
        elif other.muscle == ex.muscle:
            same_muscle.append(other)
    return sorted(same_pattern, key=rank), sorted(same_muscle, key=rank)


def _we_from(source, exercise: Exercise, position: int) -> WorkoutExercise:
    return WorkoutExercise(
        exercise_id=exercise.id,
        position=position,
        sets=source.sets,
        reps_min=source.reps_min,
        reps_max=source.reps_max,
        seconds=source.seconds,
        rest_seconds=source.rest_seconds,
        superset=source.superset,
    )


def start(
    db: Session,
    user: User,
    plan: WorkoutPlan | None,
    *,
    day: date | None = None,
    away: bool = False,
    at: datetime | None = None,
) -> Workout:
    """A new workout from the plan (or empty). Away: exercises needing a gym become a substitute at hand."""
    at = at or _now()
    w = Workout(
        user_id=user.id,
        plan_id=plan.id if plan else None,
        day=day or at.date(),
        started_at=at.replace(microsecond=0),
        equipment=away_equipment() if away else None,
    )
    db.add(w)
    db.flush()
    eq = allowed(w)
    for i, item in enumerate(plan.items if plan else []):
        ex = item.exercise
        we = _we_from(item, ex, i)
        if eq is not None and ex.equipment not in eq:
            same, muscle = substitutes(db, ex, eq)
            pick = (same or muscle or [None])[0]
            if pick is not None:
                we.exercise_id, we.swapped_from_id = pick.id, ex.id
                if pick.kind == "time" and not we.seconds:
                    we.seconds = 30
        w.exercises.append(we)
    db.flush()
    return w


def _last_done(db: Session, w: Workout, ex: Exercise) -> WorkoutExercise | None:
    """This person's latest earlier time with the exercise that has sets – whatever workout it was in."""
    stmt = (
        select(WorkoutExercise)
        .join(Workout)
        .where(Workout.user_id == w.user_id, WorkoutExercise.exercise_id == ex.id, Workout.day <= w.day)
        .order_by(Workout.day.desc(), Workout.started_at.desc(), WorkoutExercise.position.desc())
        .options(selectinload(WorkoutExercise.done))
    )
    if w.id is not None:
        stmt = stmt.where(Workout.id != w.id)
    return next((we for we in db.scalars(stmt.limit(20)) if we.work), None)


def add_exercise(db: Session, w: Workout, ex: Exercise) -> WorkoutExercise:
    """Added on the spot: as many sets, the same rep range and rest as the person did it last time (so the
    suggestion judges progress in their range – 20 reps of a light squat are not "over 12, add weight");
    the first time 3 × 8–12."""
    top = max((e.position for e in w.exercises), default=-1)
    timed = ex.kind == "time"
    sets, lo, hi, secs = 3, None if timed else 8, None if timed else 12, 30 if timed else None
    rest = suggested_rest(ex)
    prev = _last_done(db, w, ex)
    if prev is not None:
        sets = max(prev.sets, len(prev.work))
        if timed:
            secs = prev.seconds or secs
        elif prev.workout.source == "jefit":  # imported: the range is what was done – a usual one from its top
            if prev.reps_max:
                lo, hi = range_for(prev.reps_max)
        elif prev.reps_max:
            lo, hi = prev.reps_min or prev.reps_max, prev.reps_max
        if prev.workout.source != "jefit":
            rest = prev.rest_seconds
    we = WorkoutExercise(
        exercise_id=ex.id,
        position=top + 1,
        sets=min(sets, 8),
        reps_min=lo,
        reps_max=hi,
        seconds=secs,
        rest_seconds=rest,
    )
    w.exercises.append(we)
    db.flush()
    return we


def swap(db: Session, we: WorkoutExercise, ex: Exercise) -> None:
    if we.done:
        raise ValueError("To ćwiczenie ma już zapisane serie – dodaj nowe zamiast zamieniać")
    if we.swapped_from_id is None:
        we.swapped_from_id = we.exercise_id
    we.exercise_id = ex.id
    if ex.kind == "time" and not we.seconds:
        we.seconds = 30
    db.flush()
    db.refresh(we)


def save_as_plan(db: Session, w: Workout, name: str) -> WorkoutPlan:
    """The workout as a plan to repeat: its exercises in its order, as many sets as were done (else planned),
    the supersets kept. Out of the FBW rotation – started from the list when wanted."""
    top = db.scalar(select(func.max(WorkoutPlan.position)))
    p = WorkoutPlan(name=name.strip()[:60] or "Mój trening", position=(top or 0) + 1, seed=SAVED_SEED)
    groups: dict[int, int] = {}
    for i, we in enumerate(x for x in w.exercises if x.done or w.finished_at is None):
        ss = groups.setdefault(we.superset, len(groups) + 1) if we.superset is not None else None
        p.items.append(
            PlanItem(
                exercise_id=we.exercise_id,
                position=i,
                sets=max(len(we.work), we.sets),
                reps_min=we.reps_min,
                reps_max=we.reps_max,
                seconds=we.seconds,
                rest_seconds=we.rest_seconds,
                superset=ss,
            )
        )
    db.add(p)
    db.flush()
    if w.plan_id is None:
        w.plan_id = p.id
    db.flush()
    return p


def round_members(w: Workout, group: int | None) -> list[WorkoutExercise]:
    """A superset whose next round can be logged at once: all its exercises at the same set, none complete."""
    if group is None or w.finished_at is not None:
        return []
    ms = [e for e in w.exercises if e.superset == group]
    if len(ms) < 2 or len({len(e.work) for e in ms}) != 1 or any(len(e.work) >= e.sets for e in ms):
        return []
    return ms


def following(w: Workout, we: WorkoutExercise) -> WorkoutExercise | None:
    """The exercise after this one on the list (None for the last)."""
    exs = list(w.exercises)
    i = exs.index(we)
    return exs[i + 1] if i + 1 < len(exs) else None


def link_superset(db: Session, w: Workout, we: WorkoutExercise, nxt: WorkoutExercise) -> None:
    """Two neighbours on the list into one superset (back and forth, the rest after the last of it). Either
    already in one: the other joins it; both in different ones: they become one."""
    group = we.superset or nxt.superset or max((e.superset or 0 for e in w.exercises), default=0) + 1
    merged = {g for g in (we.superset, nxt.superset) if g is not None}
    for e in w.exercises:
        if e is we or e is nxt or (e.superset is not None and e.superset in merged):
            e.superset = group
    db.flush()


def unlink_superset(db: Session, w: Workout, group: int | None) -> None:
    """The whole superset back to separate exercises."""
    if group is None:
        return
    for e in w.exercises:
        if e.superset == group:
            e.superset = None
    db.flush()


def preceding(w: Workout, we: WorkoutExercise) -> WorkoutExercise | None:
    """The exercise before this one on the list (None for the first)."""
    exs = list(w.exercises)
    i = exs.index(we)
    return exs[i - 1] if i > 0 else None


def blocks(w: Workout) -> list[list[WorkoutExercise]]:
    """The list in moving units: an exercise alone, or a whole superset (its members stand next to each other)."""
    out: list[list[WorkoutExercise]] = []
    for e in w.exercises:
        if out and e.superset is not None and out[-1][0].superset == e.superset:
            out[-1].append(e)
        else:
            out.append([e])
    return out


def neighbour_block(w: Workout, we: WorkoutExercise, d: int) -> list[WorkoutExercise] | None:
    """The unit next to this exercise's one, up (d=-1) or down (d=1)."""
    bs = blocks(w)
    i = next(k for k, b in enumerate(bs) if we in b)
    j = i + (1 if d > 0 else -1)
    return bs[j] if 0 <= j < len(bs) else None


def move(db: Session, w: Workout, we: WorkoutExercise, d: int) -> bool:
    """↑ / ↓ – the exercise (with its whole superset) swaps places with the unit next to it."""
    bs = blocks(w)
    i = next(k for k, b in enumerate(bs) if we in b)
    j = i + (1 if d > 0 else -1)
    if not 0 <= j < len(bs):
        return False
    bs[i], bs[j] = bs[j], bs[i]
    order = [e for b in bs for e in b]
    for pos, e in enumerate(order):
        e.position = pos
    w.exercises.sort(key=lambda e: e.position)
    db.flush()
    return True


MAX_SETS = 20


def change_sets(db: Session, we: WorkoutExercise, delta: int) -> None:
    """− / + one planned set in this workout – never below the sets already done, never below one."""
    we.sets = min(MAX_SETS, max(1, len(we.work), we.sets + delta))
    db.flush()


def uneven_sets(w: Workout, group: int | None) -> int | None:
    """A superset whose members plan different numbers of sets: the number to even them to (the smallest,
    but not below what one of them already did) – None when they are even or it would change nothing."""
    ms = [e for e in w.exercises if group is not None and e.superset == group]
    if len(ms) < 2 or len({e.sets for e in ms}) == 1:
        return None
    return max(min(e.sets for e in ms), *(len(e.work) for e in ms))


def even_sets(db: Session, w: Workout, group: int) -> None:
    """⚖ Every exercise of the superset gets the same number of sets – rounds go together."""
    n = uneven_sets(w, group)
    if n is None:
        return
    for e in w.exercises:
        if e.superset == group:
            e.sets = n
    db.flush()


def _rest_after(we: WorkoutExercise) -> int:
    """Inside a superset the rest comes after its last exercise; until then straight to the partner."""
    return we.rest_seconds if rest_holder(we) else 0


def log_set(
    db: Session,
    we: WorkoutExercise,
    *,
    weight: float | None,
    reps: int | None,
    seconds: int | None = None,
    at: datetime | None = None,
    warmup: bool = False,
    failure: bool = False,
    drops: list[dict[str, Any]] | None = None,
) -> WorkoutSet:
    at = (at or _now()).replace(microsecond=0)
    s = WorkoutSet(
        set_no=len(we.done) + 1,
        weight=weight,
        reps=reps,
        seconds=seconds,
        done_at=at,
        warmup=warmup,
        failure=failure and not warmup,
        drops=(drops or None) if not warmup else None,
    )
    we.done.append(s)
    w = we.workout
    rest = _rest_after(we)
    if w.finished_at is None and w.day == at.date() and rest:
        w.rest_until, w.rest_pushed = at + timedelta(seconds=rest), False
    else:
        w.rest_until = None
    db.flush()
    return s


REST_STEP = 15


def rest_holder(we: WorkoutExercise) -> bool:
    """Whether this exercise's rest_seconds is the rest that comes after it (inside a superset only the last one's)."""
    if we.superset is None:
        return True
    partners = [e for e in we.workout.exercises if e.superset == we.superset]
    return bool(partners) and partners[-1].id == we.id


def plan_item_for(we: WorkoutExercise) -> PlanItem | None:
    """The plan's item this exercise came from – by the exercise, or by the one it was swapped from."""
    plan = we.workout.plan
    if plan is None:
        return None
    ids = {we.exercise_id, we.swapped_from_id} - {None}
    return next((it for it in plan.items if it.exercise_id in ids), None)


def set_exercise_rest(db: Session, we: WorkoutExercise, delta: int, *, to_plan: bool = False) -> None:
    """⏱ −15 s / +15 s on an exercise during the workout, before its rest starts: its next rests in this workout
    get the new length; 📌 also writes it into the plan, so the next workout from it starts with it."""
    if delta:
        we.rest_seconds = min(900, max(0, (we.rest_seconds or 0) + delta))
    if to_plan and (it := plan_item_for(we)) is not None:
        it.rest_seconds = we.rest_seconds
    db.flush()


def adjust_rest(db: Session, w: Workout, delta: int, at: datetime | None = None) -> None:
    """−15 s / +15 s during a rest: the current one moves, and the exercise that started it keeps the new length
    for its next rests in this workout (the plan stays as it is). +15 s after the rest ran out starts it again."""
    at = (at or _now()).replace(microsecond=0)
    if w.rest_until is None or w.finished_at is not None:
        return
    base = max(w.rest_until, at) if delta > 0 else w.rest_until
    w.rest_until = max(at, base + timedelta(seconds=delta))
    if w.rest_until > at:
        w.rest_pushed = False  # the push "koniec przerwy" comes at the new end
    sets = [s for we in w.exercises for s in we.done]
    if sets:
        we = max(sets, key=lambda s: (s.done_at, s.id)).workout_exercise
        we.rest_seconds = min(900, max(0, (we.rest_seconds or 0) + delta))
    db.flush()


def undo_set(db: Session, s: WorkoutSet) -> None:
    we = s.workout_exercise
    we.done.remove(s)
    for i, other in enumerate(we.done, 1):
        other.set_no = i
    we.workout.rest_until = None
    db.flush()


def finish(db: Session, w: Workout, at: datetime | None = None) -> None:
    w.finished_at = (at or _now()).replace(microsecond=0)
    w.rest_until = None
    # an exercise nobody did leaves no empty row in the history
    for we in list(w.exercises):
        if not we.done:
            w.exercises.remove(we)
    db.flush()


def counts(w: Workout) -> bool:
    return any(we.done for we in w.exercises)


# ---------------------------------------------------------------- progression


def last_sets(db: Session, user: User, exercise_id: int, *, before: Workout | None = None) -> list[WorkoutSet]:
    """The sets of this exercise in the latest earlier workout that has any."""
    stmt = (
        select(WorkoutExercise)
        .join(Workout)
        .where(Workout.user_id == user.id, WorkoutExercise.exercise_id == exercise_id)
        .order_by(Workout.day.desc(), Workout.started_at.desc(), WorkoutExercise.position.desc())
        .options(selectinload(WorkoutExercise.done))
    )
    if before is not None:
        stmt = stmt.where(Workout.id != before.id, Workout.day <= before.day)
    for we in db.scalars(stmt.limit(20)):
        if we.work:
            return we.work
    return []


def fmt_kg(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g}".replace(".", ",")


def sets_text(sets: list[WorkoutSet]) -> str:
    """'22,5 kg × 12, 11, 10' – grouped by weight; seconds as '45 s' (a loaded hold: '24 kg × 40 s, 35 s')."""
    if not sets:
        return ""
    timed = all(s.seconds is not None and s.reps is None for s in sets)
    parts: list[str] = []
    for s in sets:
        reps = f"{s.seconds} s" if timed else str(s.reps if s.reps is not None else "–")
        reps += ("🔥" if s.failure else "") + ("↘" if s.drops else "")
        label = f"{fmt_kg(s.weight)} kg × " if s.weight else ""
        if parts and parts[-1][0] == label:
            parts[-1][1].append(reps)
        else:
            parts.append((label, [reps]))
    return " · ".join(f"{label}{', '.join(r)}" for label, r in parts)


@dataclass
class Suggestion:
    weight: float | None = None
    reps: int | None = None
    seconds: int | None = None
    last: str = ""  # "ostatnio 20 kg × 12, 12, 11, 10"
    up: bool = False  # the top of the range was hit – more weight today
    note: str = ""


def suggest(db: Session, user: User, we: WorkoutExercise) -> Suggestion:
    ex = we.exercise
    prev = last_sets(db, user, ex.id, before=we.workout)
    if ex.kind == "time":
        best = max((s.seconds or 0 for s in prev), default=0)
        target = max(best, we.seconds or 0) or 30
        hit = bool(prev) and all((s.seconds or 0) >= target for s in prev)
        weight = max((s.weight for s in prev if s.weight), default=None) if ex.step else None
        return Suggestion(
            weight=weight,
            seconds=target + 5 if hit else target,
            last=sets_text(prev),
            up=hit,
            note="+5 s" if hit else "",
        )
    if not prev:
        return Suggestion(reps=we.reps_max, note="pierwszy raz – dobierz ciężar" if ex.step else "pierwszy raz")
    weights = [s.weight for s in prev if s.weight]
    top = max(weights) if weights else None
    work = [s for s in prev if s.weight == top] if top else prev
    lo, hi = we.reps_min, we.reps_max
    # a set to failure: the top of the range was all there was – no more weight (or reps) yet
    hit = (
        hi is not None
        and len(work) >= we.sets
        and all((s.reps or 0) >= hi for s in work)
        and not any(s.failure for s in work)
    )
    if hit and top and ex.step:
        return Suggestion(weight=top + ex.step, reps=lo, last=sets_text(prev), up=True, note=f"+{fmt_kg(ex.step)} kg")
    if hit:
        return Suggestion(weight=top, reps=(hi or 0) + 1, last=sets_text(prev), up=True, note="więcej powtórzeń")
    worst = min((s.reps or 0) for s in work) if work else None
    goal = min(hi, worst + 1) if hi and worst is not None else worst
    return Suggestion(weight=top, reps=goal, last=sets_text(prev))


def e1rm(s: WorkoutSet) -> float:
    return (s.weight or 0) * (1 + (s.reps or 0) / 30)


def score(s: WorkoutSet, ex: Exercise) -> float:
    """How good a set is, comparable within one exercise: the estimated 1RM with a weight, else the reps;
    a hold – its seconds (a loaded one: the weight, then the seconds)."""
    if ex.kind == "time":
        return (s.weight or 0) * 1000 + (s.seconds or 0) if ex.step else float(s.seconds or 0)
    return e1rm(s) if s.weight else float(s.reps or 0)


def prior_best(db: Session, w: Workout) -> dict[int, float]:
    """exercise id → the best score of this person's sets before this workout (exercises done before only)."""
    ids = {we.exercise_id for we in w.exercises}
    if not ids:
        return {}
    rows = db.execute(
        select(WorkoutExercise.exercise_id, WorkoutSet)
        .join(WorkoutSet.workout_exercise)
        .join(WorkoutExercise.workout)
        .where(
            Workout.user_id == w.user_id,
            Workout.id != w.id,
            WorkoutExercise.exercise_id.in_(ids),
            WorkoutSet.warmup.is_(False),
            (Workout.day < w.day) | ((Workout.day == w.day) & (Workout.started_at < w.started_at)),
        )
    ).all()
    exs = {we.exercise_id: we.exercise for we in w.exercises}
    best: dict[int, float] = {}
    for eid, s in rows:
        v = score(s, exs[eid])
        if v > best.get(eid, 0):
            best[eid] = v
    return best


def records(w: Workout, prior: dict[int, float]) -> dict[int, WorkoutSet]:
    """🏆 workout exercise id → its best set when that beats everything before (done before at least once)."""
    out: dict[int, WorkoutSet] = {}
    for we in w.exercises:
        before = prior.get(we.exercise_id)
        if not we.work or not before:
            continue
        top = max(we.work, key=lambda s: score(s, we.exercise))
        if score(top, we.exercise) > before + 1e-9:
            out[we.id] = top
    return out


def set_text(s: WorkoutSet) -> str:
    """'30 kg × 10 🔥 ↘ 22 kg × 6' – one set with its marks and drops."""
    kg = f"{fmt_kg(s.weight)} kg × " if s.weight else ""
    text = kg + (str(s.reps) if s.reps is not None else f"{s.seconds} s")
    if s.failure:
        text += " 🔥"
    for d in s.drops or []:
        text += f" ↘ {fmt_kg(d.get('w')) + ' kg × ' if d.get('w') else ''}{d.get('r')}"
    return text


def drop_rows(form, suffix: str = "") -> list[dict[str, Any]]:
    """↘ the drops of a set from a form: drop_w{suffix} / drop_r{suffix} pairs, the empty ones skipped."""
    weights, reps = form.getlist(f"drop_w{suffix}"), form.getlist(f"drop_r{suffix}")
    out = []
    for w_raw, r_raw in itertools.zip_longest(weights, reps):
        w_txt, r_txt = str(w_raw or "").strip().replace(",", "."), str(r_raw or "").strip()
        if not r_txt:
            continue
        try:
            r, w = int(float(r_txt)), float(w_txt) if w_txt else None
        except ValueError:
            raise ValueError("Drop set: wpisz liczby") from None
        if not 0 < r <= 1000 or (w is not None and not 0 <= w <= 1000):
            raise ValueError("Drop set: powtórzenia od 1, ciężar do 1000")
        out.append({"w": w, "r": r})
    return out[:5]


def volume(w: Workout) -> float:
    """kg × reps over the weighted working sets and their drops, as logged (a dumbbell counts once)."""
    return sum(
        (s.weight or 0) * (s.reps or 0) + sum((d.get("w") or 0) * (d.get("r") or 0) for d in s.drops or [])
        for we in w.exercises
        for s in we.work
    )


@dataclass
class Summary:
    minutes: int | None
    sets: int
    volume: float
    prev_volume: float | None  # the previous time of the same plan
    prev_day: date | None
    records: list[tuple[str, str]]  # (exercise, its best set)

    @property
    def change(self) -> float | None:
        if not self.prev_volume or not self.volume:
            return None
        return (self.volume - self.prev_volume) / self.prev_volume * 100


def summary(db: Session, w: Workout, recs: dict[int, WorkoutSet]) -> Summary:
    """The card on top of a finished workout: how long, how much, against last time, the records."""
    minutes = None
    if w.finished_at and w.source == "app":
        m = round((w.finished_at - w.started_at).total_seconds() / 60)
        minutes = m if 0 < m <= 240 else None  # a workout finished the next morning has no honest length
    prev = None
    if w.plan_id:
        prev = db.scalar(
            select(Workout)
            .where(
                Workout.user_id == w.user_id,
                Workout.plan_id == w.plan_id,
                Workout.id != w.id,
                Workout.finished_at.is_not(None),
                (Workout.day < w.day) | ((Workout.day == w.day) & (Workout.started_at < w.started_at)),
            )
            .order_by(Workout.day.desc(), Workout.started_at.desc())
            .limit(1)
        )
    return Summary(
        minutes=minutes,
        sets=sum(len(we.work) for we in w.exercises),
        volume=volume(w),
        prev_volume=volume(prev) if prev else None,
        prev_day=prev.day if prev else None,
        records=[(we.exercise.name, set_text(recs[we.id])) for we in w.exercises if we.id in recs],
    )


@dataclass
class HistoryRow:
    day: date
    sets: list[WorkoutSet]
    best: float  # estimated 1RM of the session, kg
    text: str


def history(
    db: Session, user: User, exercise_id: int, limit: int | None = 30, since: date | None = None
) -> list[HistoryRow]:
    stmt = (
        select(WorkoutExercise)
        .join(Workout)
        .where(Workout.user_id == user.id, WorkoutExercise.exercise_id == exercise_id)
        .order_by(Workout.day.desc(), Workout.started_at.desc())
        .options(selectinload(WorkoutExercise.done), selectinload(WorkoutExercise.workout))
    )
    if since is not None:
        stmt = stmt.where(Workout.day >= since)
    rows = db.scalars(stmt.limit(limit) if limit else stmt)
    out = []
    for we in rows:
        if we.work:
            out.append(HistoryRow(we.workout.day, we.work, max(e1rm(s) for s in we.work), sets_text(we.work)))
    return out


def done_exercises(db: Session, user: User) -> list[tuple[Exercise, date, int]]:
    """Exercises this person ever did: (exercise, last day, sessions) – newest first."""
    rows = db.execute(
        select(WorkoutExercise.exercise_id, func.max(Workout.day), func.count(func.distinct(Workout.id)))
        .join(Workout)
        .join(WorkoutSet)
        .where(Workout.user_id == user.id)
        .group_by(WorkoutExercise.exercise_id)
    ).all()
    out = [(db.get(Exercise, eid), last, n) for eid, last, n in rows]
    return sorted(out, key=lambda r: (r[1], r[0].name), reverse=True)


def best_e1rm(db: Session, user: User) -> dict[int, float]:
    """exercise id → this person's best estimated 1RM ever (weighted sets only)."""
    rows = db.execute(
        select(WorkoutExercise.exercise_id, func.max(WorkoutSet.weight * (1 + WorkoutSet.reps / 30.0)))
        .join(WorkoutSet.workout_exercise)
        .join(WorkoutExercise.workout)
        .where(
            Workout.user_id == user.id,
            WorkoutSet.weight > 0,
            WorkoutSet.reps.is_not(None),
            WorkoutSet.warmup.is_(False),
        )
        .group_by(WorkoutExercise.exercise_id)
    ).all()
    return {eid: best for eid, best in rows if best}


# ---------------------------------------------------------------- the week


def prefs(db: Session, user: User) -> TrainingPrefs:
    p = db.get(TrainingPrefs, user.id)
    if p is None:
        p = TrainingPrefs(user_id=user.id, weekly_goal=3, gap_days=3)
        db.add(p)
        db.flush()
    return p


@dataclass
class Week:
    goal: int
    done: int
    last: date | None
    days_since: int | None
    gap_days: int
    days: list[date] = field(default_factory=list)  # training days this week

    @property
    def freshness(self) -> float:
        """0 = just trained … 1 = the gap is reached (the bar of /obowiazki)."""
        if self.days_since is None:
            return 1.0
        return min(1.0, self.days_since / max(1, self.gap_days))


def training_days(db: Session, user: User, since: date) -> list[date]:
    """Days with at least one set logged (a few short sessions on one day are one training)."""
    return sorted(
        set(
            db.scalars(
                select(Workout.day)
                .join(WorkoutExercise)
                .join(WorkoutSet)
                .where(Workout.user_id == user.id, Workout.day >= since)
            )
        )
    )


def week(db: Session, user: User, today: date) -> Week:
    p = prefs(db, user)
    monday = today - timedelta(days=today.weekday())
    days = [d for d in training_days(db, user, monday) if d <= today]
    last = db.scalar(
        select(func.max(Workout.day))
        .join(WorkoutExercise)
        .join(WorkoutSet)
        .where(Workout.user_id == user.id, Workout.day <= today)
    )
    since = (today - last).days if last else None
    return Week(p.weekly_goal, len(days), last, since, p.gap_days, days)


# ---------------------------------------------------------------- pushes


def _send(db: Session, user: User, body: dict[str, Any]) -> int:
    from domownik import notify

    try:
        return 1 if notify.send(db, user, body) else 0
    except webpush.PushError as exc:
        log.warning("training push: %s – %s", user.name, exc)
        return 0


def next_up(w: Workout) -> WorkoutExercise | None:
    """What comes now. A superset goes back and forth (squat → pull-up → squat…) – its member with the fewest
    sets, from the one after the last logged; otherwise the first unfinished from the last logged one on."""
    exs = list(w.exercises)
    logged = [we for we in exs if we.done]
    if not logged:
        return next((we for we in exs if len(we.work) < we.sets), None)
    last = max(logged, key=lambda we: max(s.done_at for s in we.done))
    if last.superset is not None:
        group = [we for we in exs if we.superset == last.superset]
        i = group.index(last)
        ring = group[i + 1 :] + group[: i + 1]
        open_ = [we for we in ring if len(we.work) < we.sets]
        if open_:
            fewest = min(len(we.work) for we in open_)
            return next(we for we in open_ if len(we.work) == fewest)
        start = exs.index(group[-1]) + 1
    else:
        start = exs.index(last) if len(last.work) < last.sets else exs.index(last) + 1
    for we in exs[start:] + exs[:start]:
        if len(we.work) < we.sets:
            return we
    return None


def _next_up(w: Workout) -> str:
    we = next_up(w)
    if we is None:
        return "Wszystko zrobione – zakończ trening"
    return f"{we.exercise.name} – seria {len(we.work) + 1}/{we.sets}"


def run_rest(db: Session, at: datetime | None = None) -> int:
    """Every few seconds: a rest that has ended → a push to the phone in the pocket."""
    at = (at or _now()).replace(tzinfo=None)
    sent = 0
    due = db.scalars(
        select(Workout).where(
            Workout.rest_until.is_not(None),
            Workout.rest_until <= at,
            Workout.rest_pushed.is_(False),
            Workout.finished_at.is_(None),
        )
    )
    for w in list(due):
        w.rest_pushed = True
        db.commit()
        if at - w.rest_until > REST_PUSH_LATE:
            continue
        sent += _send(
            db,
            db.get(User, w.user_id),
            {
                "title": "⏱ Koniec przerwy",
                "body": _next_up(w),
                "url": f"/forma/trening/{w.id}",
                "tag": f"domownik-rest-{w.user_id}",
                "urgent": True,
            },
        )
    return sent


def run_nudge(db: Session, at: datetime | None = None) -> int:
    """Once a day at NUDGE_HOUR: more than gap_days without training → one push (then quiet for gap_days)."""
    at = (at or _now()).replace(tzinfo=None)
    if at.hour != NUDGE_HOUR:
        return 0
    today = at.date()
    sent = 0
    for p in list(db.scalars(select(TrainingPrefs))):
        if p.gap_pushed_on and (today - p.gap_pushed_on).days < max(1, p.gap_days):
            continue
        user = db.get(User, p.user_id)
        wk = week(db, user, today)
        if wk.days_since is None or wk.days_since <= p.gap_days or active(db, user):
            continue
        p.gap_pushed_on = today
        db.commit()
        nxt = next_plan(db, user)
        sent += _send(
            db,
            user,
            {
                "title": f"💪 {wk.days_since} dni bez treningu",
                "body": f"W tym tygodniu {wk.done}/{wk.goal}." + (f" Dziś {nxt.name}?" if nxt else ""),
                "url": "/forma",
                "tag": f"domownik-nudge-{user.id}",
            },
        )
    return sent
