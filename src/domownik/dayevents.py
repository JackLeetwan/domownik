"""📌 All-day entries of the family calendar (DayEvent): which family's turn for Christmas, a trip, a school break.

`by_day` expands them (yearly ones on every year of the window) into DayOcc per day, the way the calendar,
the "Dziś" card, the push and the ICS feed need them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from domownik.models import DayEvent

# offered in the form; any other emoji typed in is kept too
ICONS = ("📌", "🎄", "🐣", "🎉", "✈️", "🏖️", "⛰️", "🏫", "🏥", "🎂", "🚗", "🏠")


@dataclass(frozen=True)
class DayOcc:
    event: DayEvent
    starts_on: date
    ends_on: date

    @property
    def title(self) -> str:
        return self.event.title

    @property
    def icon(self) -> str:
        return self.event.icon or "📌"

    @property
    def multi(self) -> bool:
        return self.ends_on != self.starts_on

    def mine(self, user_id: int) -> bool:
        return self.event.user_id in (None, user_id)


def _in_year(d: date, year: int) -> date:
    try:
        return d.replace(year=year)
    except ValueError:  # 29 February in a common year
        return date(year, 2, 28)


def spans(ev: DayEvent, start: date, end: date) -> list[tuple[date, date]]:
    """(first, last) day of each occurrence that touches [start, end]."""
    if not ev.yearly:
        return [(ev.starts_on, ev.ends_on)] if ev.starts_on <= end and ev.ends_on >= start else []
    out = []
    for year in range(max(start.year - 1, ev.starts_on.year), end.year + 1):
        first = _in_year(ev.starts_on, year)
        last = first + timedelta(days=ev.length - 1)
        if first <= end and last >= start:
            out.append((first, last))
    return out


def load(db: Session, start: date, end: date, *, user_id: int | None = None, notify: bool = False) -> list[DayEvent]:
    q = select(DayEvent).where(
        or_(DayEvent.yearly.is_(True), (DayEvent.starts_on <= end) & (DayEvent.ends_on >= start))
    )
    if user_id is not None:
        q = q.where(or_(DayEvent.user_id.is_(None), DayEvent.user_id == user_id))
    if notify:
        q = q.where(DayEvent.notify.is_(True))
    return list(db.scalars(q))


def by_day(
    db: Session, start: date, end: date, *, user_id: int | None = None, notify: bool = False
) -> dict[date, list[DayOcc]]:
    """Occurrences per day in [start, end]; a multi-day one is listed on each of its days."""
    out: dict[date, list[DayOcc]] = defaultdict(list)
    for ev in load(db, start, end, user_id=user_id, notify=notify):
        for first, last in spans(ev, start, end):
            occ = DayOcc(ev, first, last)
            d = max(first, start)
            while d <= min(last, end):
                out[d].append(occ)
                d += timedelta(days=1)
    for occs in out.values():
        occs.sort(key=lambda o: (o.starts_on, o.title))
    return dict(out)


def on(db: Session, day: date, *, user_id: int | None = None, notify: bool = False) -> list[DayOcc]:
    return by_day(db, day, day, user_id=user_id, notify=notify).get(day, [])


def upcoming(db: Session, today: date, *, days: int = 400) -> list[DayOcc]:
    """Everything from today on (one row per occurrence, not per day) – the list on /kalendarz/dni."""
    end = today + timedelta(days=days)
    rows = [DayOcc(ev, a, b) for ev in load(db, today, end) for a, b in spans(ev, today, end)]
    rows += [
        DayOcc(ev, ev.starts_on, ev.ends_on) for ev in db.scalars(select(DayEvent).where(DayEvent.starts_on > end))
    ]
    return sorted(rows, key=lambda o: (o.starts_on, o.title))
