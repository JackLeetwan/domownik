"""Week schedule (/grafik): fixed weekly slots – classes, kindergarten pick-ups, swimming, cleaners.

An event repeats on chosen weekdays within an optional period (school year); a single occurrence can
be called off. Who takes care of an occurrence: one person, everyone ("wspólne"), a person per weekday,
or two people alternating every week (see ScheduleEvent).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domownik.models import ScheduleEvent, ScheduleSkip, User

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DAY_NAMES = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")
DAY_SHORT = ("pn", "wt", "śr", "czw", "pt", "sob", "nd")


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parse_time(raw: str | None) -> str | None:
    """'7:5' is not accepted, '7:05' → '07:05'. Empty → None; anything else → ValueError."""
    value = (raw or "").strip().replace(".", ":")
    if not value:
        return None
    m = TIME_RE.match(value)
    if not m:
        raise ValueError("Godzina w formacie GG:MM, np. 16:30")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def days_label(days: list[int]) -> str:
    """[0,1,2,3,4] → 'pn–pt', [1,3] → 'wt, czw', all → 'codziennie'."""
    if len(days) == 7:
        return "codziennie"
    if len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
        return f"{DAY_SHORT[days[0]]}–{DAY_SHORT[days[-1]]}"
    return ", ".join(DAY_SHORT[d] for d in days)


def responsible_id(ev: ScheduleEvent, day: date) -> int | None:
    """The account taking care of the occurrence on `day`; None = shared by everyone."""
    if ev.assign_mode == "days":
        return responsible_id_for_weekday(ev, day.weekday())
    if ev.assign_mode == "weeks":
        anchor = week_start(ev.rota_anchor or ev.starts_on or day)
        odd = ((week_start(day) - anchor).days // 7) % 2
        return ev.alt_assignee_id if odd else ev.assignee_id
    return ev.assignee_id


def active_on(ev: ScheduleEvent, day: date) -> bool:
    return (
        day.weekday() in ev.days
        and (ev.starts_on is None or day >= ev.starts_on)
        and (ev.ends_on is None or day <= ev.ends_on)
    )


@dataclass
class Occurrence:
    event: ScheduleEvent
    day: date
    assignee: User | None
    cancelled: bool

    @property
    def sort_key(self) -> tuple:
        return (self.day, self.event.starts_at, self.event.title.lower(), self.event.id)

    def mine(self, user_id: int) -> bool:
        """'Moje': mine or shared."""
        return self.assignee is None or self.assignee.id == user_id


def load_events(db: Session) -> list[ScheduleEvent]:
    stmt = select(ScheduleEvent).options(
        selectinload(ScheduleEvent.skips), selectinload(ScheduleEvent.item), selectinload(ScheduleEvent.assignee)
    )
    return list(db.scalars(stmt.order_by(ScheduleEvent.starts_at, ScheduleEvent.title)))


def occurrences(
    db: Session,
    start: date,
    end: date,
    *,
    user_id: int | None = None,
    cancelled: bool = True,
    events: list[ScheduleEvent] | None = None,
) -> list[Occurrence]:
    """Every occurrence in [start, end] by day and time.

    user_id → only that person's and the shared ones ("Moje"); cancelled=False → called-off ones left out.
    """
    events = load_events(db) if events is None else events
    users = {u.id: u for u in db.scalars(select(User))}
    out: list[Occurrence] = []
    for ev in events:
        skipped = {s.on_date for s in ev.skips}
        d = start
        while d <= end:
            if active_on(ev, d):
                who = responsible_id(ev, d)
                occ = Occurrence(ev, d, users.get(who) if who else None, d in skipped)
                if (cancelled or not occ.cancelled) and (user_id is None or occ.mine(user_id)):
                    out.append(occ)
            d += timedelta(days=1)
    out.sort(key=lambda o: o.sort_key)
    return out


def toggle_skip(db: Session, ev: ScheduleEvent, day: date, user: User | None) -> bool:
    """Call one occurrence off, or bring it back. Returns True when it is now called off."""
    if not active_on(ev, day):
        raise ValueError("Tego dnia nie ma tego wydarzenia")
    for s in ev.skips:
        if s.on_date == day:
            ev.skips.remove(s)
            return False
    ev.skips.append(ScheduleSkip(on_date=day, created_by_id=user.id if user else None))
    return True


def who_label(ev: ScheduleEvent, users: dict[int, User]) -> str:
    """Short description of who takes care of it, for the list of all events."""

    def name(uid: int | None) -> str:
        return users[uid].name if uid in users else "wspólne"

    if ev.assign_mode == "days":
        parts = [f"{DAY_SHORT[d]} {name(responsible_id_for_weekday(ev, d))}" for d in ev.days]
        return ", ".join(parts)
    if ev.assign_mode == "weeks":
        return f"naprzemiennie: {name(ev.assignee_id)} / {name(ev.alt_assignee_id)}"
    return name(ev.assignee_id)


def responsible_id_for_weekday(ev: ScheduleEvent, weekday: int) -> int | None:
    raw = (ev.day_assignees or {}).get(str(weekday))
    return int(raw) if raw not in (None, "") else None


def on_duty_this_week(ev: ScheduleEvent, today: date) -> tuple[int | None, int | None]:
    """weeks mode: (this week's person, next week's person) – how the form shows the rota."""
    a = responsible_id(ev, today)
    b = ev.alt_assignee_id if a == ev.assignee_id else ev.assignee_id
    return a, b
