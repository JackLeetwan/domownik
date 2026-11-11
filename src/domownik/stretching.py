"""🤸 Stretching in "Forma": the library of stretches and saved routines ("Przed treningiem", "Po treningu").

The library is shared by the household and seeded from catalog/seed/stretches.yaml (new keys are added on start,
names and defaults follow the catalog). Routines are shared too, like the strength plans; the two default ones
are made on the first visit and then changed freely.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domownik import training as tr
from domownik.config import now
from domownik.models import Stretch, StretchRoutine, StretchRoutineItem, StretchSession, User, Workout

CATALOG = Path(__file__).parent / "catalog" / "seed" / "stretches.yaml"
TRANSITION = 5  # seconds to get into the position: before every stretch and before its second side


@functools.cache
def catalog() -> dict[str, Any]:
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))


@functools.cache
def _by_key() -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in catalog()["stretches"]}


def type_label(key: str) -> str:
    return catalog()["types"].get(key, key)


def area_label(key: str) -> str:
    return catalog()["areas"].get(key, key)


# ---------------------------------------------------------------- pictures and instructions


def _frames(s: Stretch) -> tuple[str, str] | None:
    row = _by_key().get(s.key)
    if not row or not row.get("img"):
        return None
    slug = tr._slug(row["img"])
    return f"/static/exercises/{slug}.webp", f"/static/exercises/{slug}-1.webp"


def image(s: Stretch) -> str | None:
    """The icon: the frame that shows the stretch (the start of some is just a person standing)."""
    frames = _frames(s)
    if frames is None:
        return None
    return frames[1] if _by_key()[s.key].get("pic") == 1 else frames[0]


def image_start(s: Stretch) -> str | None:
    frames = _frames(s)
    return frames[0] if frames else None


def image_end(s: Stretch) -> str | None:
    frames = _frames(s)
    return frames[1] if frames else None


def icon(s: Stretch) -> str:
    return "🤸" if s.type == "dynamic" else "🧘"


def guide(s: Stretch) -> tr.Guide | None:
    return tr.stretch_guides().get(s.key)


# ---------------------------------------------------------------- library and routines


def ensure_library(db: Session) -> None:
    """Seeded stretches missing in the database are added; the existing ones follow the catalog."""
    have = {s.key: s for s in db.scalars(select(Stretch))}
    for row in catalog()["stretches"]:
        s = have.get(row["key"]) or Stretch(key=row["key"])
        s.name, s.type, s.area = row["name"], row["type"], row["area"]
        s.sided, s.seconds = bool(row.get("sided")), int(row["seconds"])
        if s.id is None:
            db.add(s)
    db.flush()


def seed_routines(db: Session) -> list[StretchRoutine]:
    """The default routines – once, when there are none at all (also none archived)."""
    if db.scalar(select(StretchRoutine.id).limit(1)) is not None:
        return []
    ensure_library(db)
    by_key = {s.key: s for s in db.scalars(select(Stretch))}
    made = []
    for pos, row in enumerate(catalog()["routines"]):
        r = StretchRoutine(name=row["name"], kind=row["kind"], position=pos)
        r.items = [StretchRoutineItem(stretch=by_key[k], position=i) for i, k in enumerate(row["items"])]
        db.add(r)
        made.append(r)
    db.flush()
    return made


def prepare(db: Session) -> None:
    ensure_library(db)
    seed_routines(db)


def routines(db: Session) -> list[StretchRoutine]:
    return list(
        db.scalars(
            select(StretchRoutine)
            .where(StretchRoutine.archived.is_(False))
            .options(selectinload(StretchRoutine.items).selectinload(StretchRoutineItem.stretch))
            .order_by(StretchRoutine.position, StretchRoutine.id)
        )
    )


def for_kind(db: Session, kind: str) -> StretchRoutine | None:
    """The routine the workout offers before / after (the first one of that kind)."""
    return next((r for r in routines(db) if r.kind == kind), None)


def library(db: Session) -> list[Stretch]:
    order = {row["key"]: i for i, row in enumerate(catalog()["stretches"])}
    return sorted(db.scalars(select(Stretch)), key=lambda s: order.get(s.key, len(order)))


def groups(db: Session, type_: str | None) -> list[tuple[str, list[Stretch]]]:
    """The picker: stretches by area (in the catalog's order of areas), optionally only dynamic / static."""
    rows = [s for s in library(db) if type_ is None or s.type == type_]
    return [(label, [s for s in rows if s.area == k]) for k, label in catalog()["areas"].items()]


def steps_count(item: StretchRoutineItem) -> int:
    return 2 if item.stretch.sided else 1


def total_seconds(r: StretchRoutine) -> int:
    """How long the routine takes: every side of every stretch, each with a transition before it."""
    return sum((it.duration + TRANSITION) * steps_count(it) for it in r.items)


def fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    if not m:
        return f"{s} s"
    return f"{m} min" if not s else f"{m} min {s} s"


# ---------------------------------------------------------------- sessions (the timer runs in the browser)


def snapshot(r: StretchRoutine) -> list[dict[str, Any]]:
    """The routine for the timer – as it is now (editing it during the session changes nothing)."""
    return [
        {
            "id": it.stretch.id,
            "name": it.stretch.name,
            "seconds": it.duration,
            "sided": it.stretch.sided,
            "img": image(it.stretch),
        }
        for it in r.items
    ]


def start(db: Session, user: User, r: StretchRoutine, workout: Workout | None = None) -> StretchSession:
    """A new session; an unfinished one of the same person is dropped (one at a time, nothing to resume)."""
    for old in db.scalars(
        select(StretchSession).where(StretchSession.user_id == user.id, StretchSession.finished_at.is_(None))
    ):
        db.delete(old)
    steps = snapshot(r)
    s = StretchSession(
        user_id=user.id,
        routine_id=r.id,
        workout_id=workout.id if workout else None,
        name=r.name,
        steps=steps,
        total=len(steps),
        started_at=now().replace(tzinfo=None, microsecond=0),
    )
    db.add(s)
    db.flush()
    return s


def finish(s: StretchSession, done: int, seconds: int) -> None:
    s.done = max(0, min(done, s.total))
    s.seconds = max(0, min(seconds, 4 * 3600))
    s.finished_at = now().replace(tzinfo=None, microsecond=0)


def get_own(db: Session, user: User, session_id: int) -> StretchSession | None:
    s = db.get(StretchSession, session_id)
    return s if s is not None and s.user_id == user.id else None


def recent(db: Session, user: User, limit: int = 5) -> list[StretchSession]:
    return list(
        db.scalars(
            select(StretchSession)
            .where(StretchSession.user_id == user.id, StretchSession.finished_at.is_not(None))
            .order_by(StretchSession.started_at.desc())
            .limit(limit)
        )
    )


# ---------------------------------------------------------------- with a workout: before / after


@dataclass
class Offer:
    """What the workout screen shows about stretching: a routine to start (or the started one to go back to)
    and the sessions already done with this workout."""

    kind: str | None = None  # before | after – which card
    routine: StretchRoutine | None = None
    pending: StretchSession | None = None  # started from this workout, not finished
    done: list[StretchSession] = field(default_factory=list)


def for_workout(db: Session, user: User, w: Workout) -> list[StretchSession]:
    return list(
        db.scalars(
            select(StretchSession)
            .where(StretchSession.user_id == user.id, StretchSession.workout_id == w.id)
            .order_by(StretchSession.started_at)
        )
    )


def offer(db: Session, user: User, w: Workout, sets_done: int) -> Offer:
    """Before: today's workout with no set done yet and no warm-up done. After: today's finished workout with
    no stretching since it ended. The routine is the first one of that kind (the list order)."""
    sessions = for_workout(db, user, w)
    o = Offer(done=[s for s in sessions if s.finished_at is not None])
    if w.day != now().date():
        return o
    if w.finished_at is None:
        if sets_done == 0 and not o.done:
            o.kind = "before"
    elif not any(s.started_at >= w.finished_at for s in o.done):
        o.kind = "after"
    if o.kind:
        prepare(db)  # the default routines on the first look – kept, the start form posts their id
        db.commit()
        r = for_kind(db, o.kind)
        o.routine = r if r is not None and r.items else None
        o.pending = next((s for s in sessions if s.finished_at is None), None)
        if o.routine is None and o.pending is None:
            o.kind = None
    return o
