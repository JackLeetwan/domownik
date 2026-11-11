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
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from domownik import webpush
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


def ensure_library(db: Session) -> None:
    """Seeded exercises missing in the database are added; their Jefit names follow the catalog."""
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
            ex.en = en
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
                    rest_seconds=it.get("rest", DEFAULT_REST),
                    superset=it.get("superset"),
                )
            )
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


def active(db: Session, user: User) -> Workout | None:
    """The workout still going (started, not finished) – the newest one."""
    return db.scalar(
        select(Workout)
        .where(Workout.user_id == user.id, Workout.finished_at.is_(None))
        .order_by(Workout.started_at.desc())
        .limit(1)
    )


def _off_rotation() -> set[str]:
    """Catalog plans done on occasion (`rotation: false` – the one for two), not in the FBW 1 → 2 → 3 cycle."""
    return {p["key"] for p in catalog()["plans"] if p.get("rotation") is False}


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


def add_exercise(db: Session, w: Workout, ex: Exercise) -> WorkoutExercise:
    top = max((e.position for e in w.exercises), default=-1)
    we = WorkoutExercise(
        exercise_id=ex.id,
        position=top + 1,
        sets=3,
        reps_min=None if ex.kind == "time" else 8,
        reps_max=None if ex.kind == "time" else 12,
        seconds=30 if ex.kind == "time" else None,
        rest_seconds=DEFAULT_REST,
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


def _rest_after(we: WorkoutExercise) -> int:
    """Inside a superset the rest comes after its last exercise; until then straight to the partner."""
    if we.superset is None:
        return we.rest_seconds
    partners = [e for e in we.workout.exercises if e.superset == we.superset]
    return we.rest_seconds if partners and partners[-1].id == we.id else 0


def log_set(
    db: Session,
    we: WorkoutExercise,
    *,
    weight: float | None,
    reps: int | None,
    seconds: int | None = None,
    at: datetime | None = None,
) -> WorkoutSet:
    at = (at or _now()).replace(microsecond=0)
    s = WorkoutSet(set_no=len(we.done) + 1, weight=weight, reps=reps, seconds=seconds, done_at=at)
    we.done.append(s)
    w = we.workout
    rest = _rest_after(we)
    if w.finished_at is None and w.day == at.date() and rest:
        w.rest_until, w.rest_pushed = at + timedelta(seconds=rest), False
    else:
        w.rest_until = None
    db.flush()
    return s


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
        if we.done:
            return list(we.done)
    return []


def fmt_kg(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g}".replace(".", ",")


def sets_text(sets: list[WorkoutSet]) -> str:
    """'22,5 kg × 12, 11, 10' – grouped by weight; seconds as '45 s'."""
    if not sets:
        return ""
    if all(s.seconds is not None and s.reps is None for s in sets):
        return ", ".join(f"{s.seconds} s" for s in sets)
    parts: list[str] = []
    for s in sets:
        reps = str(s.reps if s.reps is not None else "–")
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
        return Suggestion(
            seconds=target + 5 if hit else target, last=sets_text(prev), up=hit, note="+5 s" if hit else ""
        )
    if not prev:
        return Suggestion(reps=we.reps_max, note="pierwszy raz – dobierz ciężar" if ex.step else "pierwszy raz")
    weights = [s.weight for s in prev if s.weight]
    top = max(weights) if weights else None
    work = [s for s in prev if s.weight == top] if top else prev
    lo, hi = we.reps_min, we.reps_max
    hit = hi is not None and len(work) >= we.sets and all((s.reps or 0) >= hi for s in work)
    if hit and top and ex.step:
        return Suggestion(weight=top + ex.step, reps=lo, last=sets_text(prev), up=True, note=f"+{fmt_kg(ex.step)} kg")
    if hit:
        return Suggestion(weight=top, reps=(hi or 0) + 1, last=sets_text(prev), up=True, note="więcej powtórzeń")
    worst = min((s.reps or 0) for s in work) if work else None
    goal = min(hi, worst + 1) if hi and worst is not None else worst
    return Suggestion(weight=top, reps=goal, last=sets_text(prev))


def e1rm(s: WorkoutSet) -> float:
    return (s.weight or 0) * (1 + (s.reps or 0) / 30)


@dataclass
class HistoryRow:
    day: date
    sets: list[WorkoutSet]
    best: float  # estimated 1RM of the session, kg
    text: str


def history(db: Session, user: User, exercise_id: int, limit: int = 30) -> list[HistoryRow]:
    rows = db.scalars(
        select(WorkoutExercise)
        .join(Workout)
        .where(Workout.user_id == user.id, WorkoutExercise.exercise_id == exercise_id)
        .order_by(Workout.day.desc(), Workout.started_at.desc())
        .options(selectinload(WorkoutExercise.done), selectinload(WorkoutExercise.workout))
        .limit(limit)
    )
    out = []
    for we in rows:
        if we.done:
            out.append(
                HistoryRow(we.workout.day, list(we.done), max(e1rm(s) for s in we.done), sets_text(list(we.done)))
            )
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
        return next((we for we in exs if len(we.done) < we.sets), None)
    last = max(logged, key=lambda we: max(s.done_at for s in we.done))
    if last.superset is not None:
        group = [we for we in exs if we.superset == last.superset]
        i = group.index(last)
        ring = group[i + 1 :] + group[: i + 1]
        open_ = [we for we in ring if len(we.done) < we.sets]
        if open_:
            fewest = min(len(we.done) for we in open_)
            return next(we for we in open_ if len(we.done) == fewest)
        start = exs.index(group[-1]) + 1
    else:
        start = exs.index(last) if len(last.done) < last.sets else exs.index(last) + 1
    for we in exs[start:] + exs[:start]:
        if len(we.done) < we.sets:
            return we
    return None


def _next_up(w: Workout) -> str:
    we = next_up(w)
    if we is None:
        return "Wszystko zrobione – zakończ trening"
    return f"{we.exercise.name} – seria {len(we.done) + 1}/{we.sets}"


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
