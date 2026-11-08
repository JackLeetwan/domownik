"""House chores with rotation (/obowiazki): how stale each chore is and whose turn it is.

Freshness (like Sweepy / Tody): time since the last ✓ against the interval – the bar goes from green
to red, the list is ordered by urgency. A chore never ticked off counts as due now (not overdue).

Rotation: chore.rotation is the turn order (user ids). After ✓ the turn moves to the person after the
one who actually did it – ticking off for someone else (Ola does Ala's bathroom) gives the next turn to
whoever follows Ola, so nobody is skipped twice and the work stays even. A performer outside the
rotation moves the turn on from the person whose turn it was. Undo restores the turn saved with the ✓.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domownik.db import memoized
from domownik.models import Chore, ChoreDone, PointEntry, User

SOON_SHARE = 0.75  # past 3/4 of the interval → "wkrótce"

# one-click suggestions: (name, room, every N days, minutes)
SUGGESTIONS: list[tuple[str, str, int, int | None]] = [
    ("Odkurzanie", "Cały dom", 7, 30),
    ("Łazienka", "Łazienka", 7, 30),
    ("Kuchnia – blat i zlew", "Kuchnia", 3, 10),
    ("Wyniesienie śmieci", "Kuchnia", 3, 5),
    ("Zmiana pościeli", "Sypialnia", 14, 15),
    ("Mycie lodówki", "Kuchnia", 30, 20),
    ("Mycie okien", "Cały dom", 90, 90),
    ("Pranie", "Łazienka", 3, 10),
]
TIMER_SUGGESTIONS = {"Pranie"}  # added with ⏱ 1/2/3 h on the row


def turn_id(chore: Chore) -> int | None:
    """Whose turn it is now; None = anyone (no rotation)."""
    rot = list(chore.rotation or [])
    if not rot:
        return None
    return chore.turn_user_id if chore.turn_user_id in rot else rot[0]


def next_after(rotation: list[int], user_id: int | None) -> int | None:
    rot = list(rotation or [])
    if not rot:
        return None
    if user_id in rot:
        return rot[(rot.index(user_id) + 1) % len(rot)]
    return rot[0]


@dataclass
class ChoreState:
    chore: Chore
    last: ChoreDone | None
    turn: User | None
    today: date

    @property
    def days_since(self) -> int | None:
        return (self.today - self.last.done_on).days if self.last else None

    @property
    def due_on(self) -> date | None:
        return self.last.done_on + timedelta(days=self.chore.every_days) if self.last else None

    @property
    def days_left(self) -> int | None:
        return (self.due_on - self.today).days if self.last else None

    @property
    def urgency(self) -> float:
        """Share of the interval gone by: 0 just done, 1 due today, >1 overdue. Never done = 1."""
        if self.last is None:
            return 1.0
        return max(0, self.days_since) / max(1, self.chore.every_days)

    @property
    def level(self) -> str:
        """new | ok | soon | today | overdue"""
        if self.last is None:
            return "new"
        left = self.days_left
        if left < 0:
            return "overdue"
        if left == 0:
            return "today"
        return "soon" if self.urgency >= SOON_SHARE else "ok"

    @property
    def fresh_pct(self) -> int:
        """Bar length: 100 just done … 0 due / overdue."""
        return round(max(0.0, 1.0 - self.urgency) * 100)

    @property
    def hue(self) -> int:
        """Bar colour: 120 green … 0 red."""
        return round(max(0.0, 1.0 - self.urgency) * 120)

    @property
    def overdue(self) -> bool:
        return self.level == "overdue"

    def mine(self, user_id: int) -> bool:
        """'Moje': my turn, or a chore without rotation."""
        return self.turn is None or self.turn.id == user_id


def states(db: Session, today: date, *, user_id: int | None = None) -> list[ChoreState]:
    """All chores, most urgent first. user_id → only the ones for that person ("Moje")."""
    return list(memoized(db, ("chore_states", today, user_id), lambda: _states(db, today, user_id)))


def _states(db: Session, today: date, user_id: int | None) -> list[ChoreState]:
    chores = list(db.scalars(select(Chore).options(selectinload(Chore.history).selectinload(ChoreDone.done_by))))
    users = {u.id: u for u in db.scalars(select(User))}
    out = []
    for c in chores:
        tid = turn_id(c)
        st = ChoreState(c, c.history[0] if c.history else None, users.get(tid) if tid else None, today)
        if user_id is None or st.mine(user_id):
            out.append(st)
    out.sort(
        key=lambda s: (-s.urgency, s.last is None, s.chore.name.lower(), s.chore.id)
    )  # due today before never done
    return out


def overdue_for(db: Session, user: User, today: date) -> list[ChoreState]:
    return [s for s in states(db, today, user_id=user.id) if s.overdue]


def complete(db: Session, chore: Chore, *, by: User, on: date) -> ChoreDone:
    """✓ Zrobione – record who did it and pass the turn on (see the module docstring)."""
    current = turn_id(chore)
    rec = ChoreDone(done_on=on, done_by_id=by.id, turn_user_id=current)
    chore.history.insert(0, rec)
    rot = list(chore.rotation or [])
    chore.turn_user_id = next_after(rot, by.id if by.id in rot else current)
    return rec


def undo_last(db: Session, chore: Chore) -> bool:
    """Take back the latest ✓ and give the turn back. False when there is nothing to undo."""
    if not chore.history:
        return False
    last = chore.history[0]
    chore.turn_user_id = last.turn_user_id
    if last.id is not None:
        for e in db.scalars(select(PointEntry).where(PointEntry.chore_done_id == last.id)):
            db.delete(e)  # the points of that ✓ go with it
        db.flush()
    chore.history.remove(last)
    return True


def set_rotation(chore: Chore, rotation: list[int], turn: int | None = None) -> None:
    """New turn order; the turn stays with the same person if they are still in it (or goes to `turn`)."""
    seen: list[int] = []
    for uid in rotation:
        if uid not in seen:
            seen.append(uid)
    chore.rotation = seen
    if turn in seen:
        chore.turn_user_id = turn
    elif chore.turn_user_id not in seen:
        chore.turn_user_id = seen[0] if seen else None


def missing_suggestions(db: Session) -> list[tuple[str, str, int, int | None]]:
    have = {n.lower() for n in db.scalars(select(Chore.name))}
    return [s for s in SUGGESTIONS if s[0].lower() not in have]
