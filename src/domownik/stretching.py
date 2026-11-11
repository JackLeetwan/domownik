"""🤸 Stretching in "Forma": the library of stretches and saved routines ("Przed treningiem", "Po treningu").

The library is shared by the household and seeded from catalog/seed/stretches.yaml (new keys are added on start,
names and defaults follow the catalog). Routines are shared too, like the strength plans; the two default ones
are made on the first visit and then changed freely.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from domownik import training as tr
from domownik.config import now
from domownik.models import Stretch, StretchGroup, StretchRoutine, StretchRoutineItem, StretchSession, User, Workout

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
    """The default routines from the catalog that were never made – also ones added to it later.

    One the household removed stays archived (its seed is still there) and is not made again.
    """
    made_from = set(db.scalars(select(StretchRoutine.seed).where(StretchRoutine.seed.is_not(None))))
    todo = [row for row in catalog()["routines"] if row["key"] not in made_from]
    if not todo:
        return []
    ensure_library(db)
    by_key = {s.key: s for s in db.scalars(select(Stretch))}
    last = db.scalar(select(func.max(StretchRoutine.position)))
    pos = 0 if last is None else last + 1
    made = []
    for row in todo:
        r = StretchRoutine(name=row["name"], kind=row["kind"], position=pos, seed=row["key"])
        r.items = [StretchRoutineItem(stretch=by_key[k], position=i) for i, k in enumerate(row["items"])]
        db.add(r)
        made.append(r)
        pos += 1
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


def groups(db: Session, type_: str | None, area: str | None = None) -> list[tuple[str, list[Stretch]]]:
    """The picker: stretches by area (in the catalog's order of areas), optionally only dynamic / static
    and only one area."""
    rows = [s for s in library(db) if type_ is None or s.type == type_]
    return [
        (label, [s for s in rows if s.area == k])
        for k, label in catalog()["areas"].items()
        if area is None or k == area
    ]


def areas_with(db: Session, type_: str | None) -> list[tuple[str, str, int]]:
    """The picker's area chips: (key, label, how many) of the areas that have a stretch of the chosen type."""
    rows = [s for s in library(db) if type_ is None or s.type == type_]
    counts = {k: sum(1 for s in rows if s.area == k) for k in catalog()["areas"]}
    return [(k, label, counts[k]) for k, label in catalog()["areas"].items() if counts[k]]


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


def _drop_unfinished(db: Session, user: User) -> None:
    for old in db.scalars(
        select(StretchSession).where(StretchSession.user_id == user.id, StretchSession.finished_at.is_(None))
    ):
        db.delete(old)


def start(db: Session, user: User, r: StretchRoutine, workout: Workout | None = None) -> StretchSession:
    """A new session; an unfinished one of the same person is dropped (one at a time, nothing to resume)."""
    _drop_unfinished(db, user)
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


# ---------------------------------------------------------------- 👫 together: one timer for two


def _naive_now() -> datetime:
    return now().replace(tzinfo=None, microsecond=0)


def is_live(g: StretchGroup) -> bool:
    """Somebody is still in it today and nobody ended it for everybody."""
    if (g.state or {}).get("finished") or g.created_at.date() != _naive_now().date():
        return False
    return any(s.finished_at is None for s in g.sessions)


def invite(db: Session, s: StretchSession, invitees: list[User]) -> StretchGroup:
    """The session becomes one done together (if it is not yet); each invitee gets a push with the link."""
    from domownik import together

    g = s.group
    if g is None:
        g = StretchGroup(created_by_id=s.user_id, invited=[], state={}, created_at=_naive_now())
        db.add(g)
        s.group = g
    g.invited = sorted(set(g.invited or []) | {u.id for u in invitees})
    db.flush()
    host = db.get(User, s.user_id)
    for u in invitees:
        if member(g, u) is None:
            together._send(
                db,
                u,
                {
                    "title": f"👫 {host.name if host else 'Ktoś'} zaprasza do rozciągania",
                    "body": f"{s.name} – wspólny timer. Dotknij, żeby dołączyć.",
                    "url": f"/forma/rozciaganie/razem/{g.id}",
                    "tag": f"domownik-rozciaganie-{g.id}",
                },
            )
    return g


def member(g: StretchGroup, user: User) -> StretchSession | None:
    return next((s for s in g.sessions if s.user_id == user.id), None)


def join(db: Session, g: StretchGroup, user: User) -> StretchSession:
    """One's own session on the group's timer (the same steps); one's other unfinished session is dropped."""
    mine = member(g, user)
    if mine is not None:
        return mine
    if user.id not in (g.invited or []):
        raise ValueError("To rozciąganie nie jest dla Ciebie")
    if not is_live(g):
        raise ValueError("To wspólne rozciąganie już się skończyło")
    host = g.sessions[0]
    _drop_unfinished(db, user)
    s = StretchSession(
        user_id=user.id,
        routine_id=host.routine_id,
        name=host.name,
        steps=host.steps,
        total=host.total,
        started_at=_naive_now(),
        group=g,
    )
    db.add(s)
    db.flush()
    return s


def open_invites(db: Session, user: User) -> list[StretchGroup]:
    """Stretching someone invited this person to, still going, not joined yet."""
    since = _naive_now().replace(hour=0, minute=0, second=0)
    return [
        g
        for g in db.scalars(select(StretchGroup).where(StretchGroup.created_at >= since).order_by(StretchGroup.id))
        if user.id in (g.invited or []) and member(g, user) is None and is_live(g)
    ]


STATE_KEYS = {"idx", "endsAt", "left", "started", "activeMs", "runFrom", "results", "finished"}


def set_state(g: StretchGroup, rev: int, state: dict[str, Any]) -> bool:
    """A change of the shared timer – only from the current rev (else the sender takes the newer one)."""
    if rev != g.rev:
        return False
    clean = {k: v for k, v in state.items() if k in STATE_KEYS}
    if len(str(clean)) > 4000:
        raise ValueError("Za duży stan timera")
    g.state = clean
    g.rev += 1
    return True
