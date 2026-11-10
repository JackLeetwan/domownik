"""👫 Trening razem – two (or more) people of the house training the same plan side by side.

Each person has their own Workout: own sets, weights, suggestions, rest timer and history. What they share
is a WorkoutGroup: the order of exercises (WorkoutExercise.slot is "the same place" in each workout), and
the changes to it – an exercise added or removed, a swap "for both" (a busy machine) – go to every member
still training. A swap "only for me" stays in one workout, the slot stays shared.

Privacy: the partner sees only the exercises and how many sets are done ("2/3 ✓") – never a weight or reps.

Joining: the starter invites (a push with a link); opening it joins straight away (one tap on the phone).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import training as tr
from domownik import webpush
from domownik.models import Exercise, User, Workout, WorkoutExercise, WorkoutGroup
from domownik.services import active_users

log = logging.getLogger("domownik")


class Busy(Exception):
    """The person already has their own workout going today with sets in it."""

    def __init__(self, workout: Workout):
        super().__init__("Masz już swój trening w toku")
        self.workout = workout


def partners(db: Session, user: User) -> list[User]:
    """Who can be invited: everyone else in the house."""
    return [u for u in active_users(db) if u.id != user.id]


def member(g: WorkoutGroup, user: User) -> Workout | None:
    return next((w for w in g.workouts if w.user_id == user.id), None)


def others(w: Workout) -> list[Workout]:
    """The other workouts of the group still going – the ones a shared change reaches."""
    if w.group is None:
        return []
    return [o for o in w.group.workouts if o.id != w.id and o.finished_at is None]


def _assign_slots(w: Workout) -> None:
    for we in w.exercises:
        if we.slot is None:
            we.slot = we.position


def _next_slot(g: WorkoutGroup) -> int:
    return max((we.slot or 0 for o in g.workouts for we in o.exercises), default=-1) + 1


def _send(db: Session, user: User, body: dict) -> int:
    from domownik import notify

    try:
        return 1 if notify.send(db, user, body) else 0
    except webpush.PushError as exc:
        log.warning("together push: %s – %s", user.name, exc)
        return 0


def invite(db: Session, w: Workout, invitees: list[User]) -> WorkoutGroup:
    """Make the workout one trained together (if it is not yet) and send each invitee a push to join."""
    g = w.group
    if g is None:
        g = WorkoutGroup(created_by_id=w.user_id, invited=[])
        db.add(g)
        w.group = g
        _assign_slots(w)
    g.invited = sorted(set(g.invited or []) | {u.id for u in invitees})
    db.flush()
    host = db.get(User, w.user_id)
    what = w.plan.name if w.plan else "trening"
    for u in invitees:
        if member(g, u) is None:
            _send(
                db,
                u,
                {
                    "title": f"👫 {host.name if host else 'Ktoś'} zaczyna {what}",
                    "body": "Dotknij, żeby dołączyć. Każde zapisuje swoje serie – widzicie tylko, ile serii zrobiło drugie.",
                    "url": f"/forma/razem/{g.id}",
                    "tag": f"domownik-razem-{g.id}",
                },
            )
    return g


def open_invites(db: Session, user: User, today: date) -> list[tuple[WorkoutGroup, Workout]]:
    """Trainings someone started today and invited this person to, still going, not joined yet."""
    out = []
    seen = set()
    for w in db.scalars(
        select(Workout)
        .where(
            Workout.group_id.is_not(None),
            Workout.finished_at.is_(None),
            Workout.day == today,
            Workout.user_id != user.id,
        )
        .order_by(Workout.started_at)
    ):
        g = w.group
        if g.id in seen or user.id not in (g.invited or []) or member(g, user) is not None:
            continue
        seen.add(g.id)
        out.append((g, w))
    return out


def join(db: Session, g: WorkoutGroup, user: User, *, today: date, at: datetime, replace: bool = False) -> Workout:
    """Join the group: a workout of one's own with the same exercises in the same order (as they are now).

    Raises ValueError (not invited / over) or Busy (one's own workout with sets is going – unless replace,
    which finishes it first)."""
    mine = member(g, user)
    if mine is not None:
        return mine
    if user.id not in (g.invited or []):
        raise ValueError("Ten trening nie jest dla Ciebie")
    host = next((w for w in g.workouts if w.finished_at is None and w.day == today), None)
    if host is None:
        raise ValueError("Ten wspólny trening już się skończył")
    own = tr.active(db, user)
    if own is not None:
        if tr.counts(own):
            if not replace:
                raise Busy(own)
            tr.finish(db, own, at=at)
        else:
            db.delete(own)  # an empty start – nothing lost
        db.flush()
    w = Workout(
        user_id=user.id,
        plan_id=host.plan_id,
        day=host.day,
        started_at=at.replace(microsecond=0),
        equipment=host.equipment,
        group=g,
    )
    db.add(w)
    _assign_slots(host)
    for we in host.exercises:
        copy = tr._we_from(we, we.exercise, we.position)
        copy.slot, copy.swapped_from_id = we.slot, we.swapped_from_id
        w.exercises.append(copy)
    db.flush()
    return w


# ---------------------------------------------------------------- shared changes


def _same_slot(o: Workout, slot: int | None) -> WorkoutExercise | None:
    return next((x for x in o.exercises if slot is not None and x.slot == slot), None)


def add(db: Session, w: Workout, ex: Exercise) -> WorkoutExercise:
    """Added at the end – for everybody training together."""
    if w.group is None:
        return tr.add_exercise(db, w, ex)
    slot = _next_slot(w.group)
    we = tr.add_exercise(db, w, ex)
    we.slot = slot
    for o in others(w):
        tr.add_exercise(db, o, ex).slot = slot
    db.flush()
    return we


def remove(db: Session, w: Workout, we: WorkoutExercise) -> None:
    """Out of the training – for the partner too, unless they already did sets of it (those stay theirs)."""
    slot = we.slot
    w.exercises.remove(we)
    for o in others(w):
        x = _same_slot(o, slot)
        if x is not None and not x.done:
            o.exercises.remove(x)
    db.flush()


def swap(db: Session, w: Workout, we: WorkoutExercise, ex: Exercise, *, both: bool) -> None:
    """A substitute – for both (the machine is busy) or only for oneself. A partner's exercise that already
    has sets keeps them."""
    old = we.exercise_id
    tr.swap(db, we, ex)
    if not both:
        return
    for o in others(w):
        x = _same_slot(o, we.slot)
        if x is not None and not x.done and x.exercise_id == old:
            tr.swap(db, x, ex)


# ---------------------------------------------------------------- what the partner sees


@dataclass
class SlotView:
    name: str
    done: int
    sets: int
    other_exercise: bool  # the partner does something else in this place (a swap only for them)

    @property
    def text(self) -> str:
        return f"{min(self.done, self.sets)}/{self.sets}{' ✓' if self.done >= self.sets else ''}"


@dataclass
class PartnerView:
    """Only counts and exercise names – never a weight or reps."""

    workout_id: int
    name: str
    done: int
    total: int
    finished: bool
    current: str | None
    current_slot: int | None
    slots: dict[int, SlotView] = field(default_factory=dict)


def views(w: Workout) -> list[PartnerView]:
    if w.group is None:
        return []
    mine = {we.slot: we.exercise_id for we in w.exercises}
    out = []
    for o in w.group.workouts:
        if o.id == w.id:
            continue
        nxt = tr.next_up(o) if o.finished_at is None else None
        out.append(
            PartnerView(
                workout_id=o.id,
                name=o.user.name,
                done=sum(min(len(x.done), x.sets) for x in o.exercises),
                total=sum(x.sets for x in o.exercises),
                finished=o.finished_at is not None,
                current=nxt.exercise.name if nxt else None,
                current_slot=nxt.slot if nxt else None,
                slots={
                    x.slot: SlotView(
                        x.exercise.name, len(x.done), x.sets, mine.get(x.slot) not in (None, x.exercise_id)
                    )
                    for x in o.exercises
                    if x.slot is not None
                },
            )
        )
    return out


def waiting_for(db: Session, w: Workout) -> list[User]:
    """Invited, not joined yet."""
    if w.group is None:
        return []
    joined = {o.user_id for o in w.group.workouts}
    return [u for u in (db.get(User, i) for i in w.group.invited or []) if u is not None and u.id not in joined]


def signature(w: Workout) -> str:
    """Changes when the list of one's exercises changes (a partner added / removed / swapped one) or someone
    joins – the screen then reloads itself."""
    raw = ",".join(f"{we.id}:{we.exercise_id}" for we in w.exercises)
    if w.group is not None:  # someone joined: the badges by the exercises appear
        raw += "|" + ",".join(str(o.id) for o in w.group.workouts)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]
