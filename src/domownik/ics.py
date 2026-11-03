"""Calendar subscription (/ics/<token>.ics): task due dates, the week schedule and warranty ends as iCalendar.

Calendar apps cannot log in, so the URL carries a personal random token; only its SHA-256 is stored
(lookup by hash, compared in constant time). No money in the feed – calendars sync to clouds:
just titles and the zone / item.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import schedule, services
from domownik.domain.tree import Tree
from domownik.models import Task, User, Warranty

PAST_DAYS = 30
FUTURE_DAYS = 365
TZID = "Europe/Warsaw"
SCOPES = {"all": "Wszystko", "mine": "Tylko moje i wspólne"}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,100}$")

# Central European time with its EU daylight-saving rule (last Sunday of March / October)
VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]


# ---------------------------------------------------------------- token


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(user: User) -> str:
    """A fresh link for the user – the previous one stops working at once."""
    token = secrets.token_urlsafe(32)
    user.ics_token_hash = token_hash(token)
    return token


def user_for_token(db: Session, token: str) -> User | None:
    if not TOKEN_RE.match(token or ""):
        return None
    h = token_hash(token)
    user = db.scalar(select(User).where(User.ics_token_hash == h))
    if user is None or not user.is_active or not secrets.compare_digest(user.ics_token_hash or "", h):
        return None
    return user


class RedactIcsToken(logging.Filter):
    """Access log: '/ics/<token>.ics' → '/ics/…'. The token is a password – it must not sit in logs."""

    PATTERN = re.compile(r"/ics/[^/?\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self.PATTERN.sub("/ics/…", a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = self.PATTERN.sub("/ics/…", record.msg)
        return True


def install_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactIcsToken) for f in logger.filters):
        logger.addFilter(RedactIcsToken())


# ---------------------------------------------------------------- iCalendar text


def escape(text: str) -> str:
    """TEXT value (RFC 5545 3.3.11)."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold(line: str) -> str:
    """Lines longer than 75 octets continue on the next one after CRLF + space (never inside a UTF-8 char)."""
    out: list[str] = []
    current, size, limit = "", 0, 75
    for ch in line:
        n = len(ch.encode())
        if size + n > limit:
            out.append(current)
            current, size, limit = ch, n, 74  # continuation lines start with the space
        else:
            current += ch
            size += n
    out.append(current)
    return "\r\n ".join(out)


def _day(d: date) -> str:
    return d.strftime("%Y%m%d")


def _all_day(uid: str, d: date, summary: str, description: str, stamp: str) -> list[str]:
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{_day(d)}",
        f"DTEND;VALUE=DATE:{_day(d + timedelta(days=1))}",
        f"SUMMARY:{escape(summary)}",
        *([f"DESCRIPTION:{escape(description)}"] if description else []),
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]


def _hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def _timed(uid: str, d: date, start: str, end: str | None, summary: str, description: str, stamp: str) -> list[str]:
    """Event at an hour of the day (Europe/Warsaw); without an end it lasts an hour."""
    h, m = _hhmm(start)
    begin = datetime(d.year, d.month, d.day, h, m)
    if end:
        eh, em = _hhmm(end)
        finish = datetime(d.year, d.month, d.day, eh, em)
        if finish <= begin:
            finish += timedelta(days=1)
    else:
        finish = begin + timedelta(hours=1)
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;TZID={TZID}:{begin.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={TZID}:{finish.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{escape(summary)}",
        *([f"DESCRIPTION:{escape(description)}"] if description else []),
        "END:VEVENT",
    ]


def build(db: Session, user: User, today: date, now: datetime, *, host: str = "domownik") -> str:
    start, end = today - timedelta(days=PAST_DAYS), today + timedelta(days=FUTURE_DAYS)
    mine = user.id if user.ics_scope == "mine" else None
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    tree = Tree.load(db)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Domownik//Kalendarz domowy//PL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Domownik",
        f"X-WR-TIMEZONE:{TZID}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        *VTIMEZONE,
    ]

    # warranty ends: the reminder task carries the date; a warranty without a live task gets its own event
    warranties = list(db.scalars(select(Warranty).where(Warranty.ends_on.is_not(None))))

    for p in services.calendar_points(db, tree, today, start, end, user_id=mine):
        d = date.fromisoformat(p["date"])
        icon, title, zone = p["icon"], p["title"], p["zone"]
        if p["kind"] == "due" and p.get("time"):
            start_t, _, end_t = p["time"].partition("–")
            uid = f"task-{p['task_id']}-{p['date']}@{host}"
            lines += _timed(uid, d, start_t, end_t or None, f"{icon} {title}", zone, stamp)
        elif p["kind"] == "due":
            lines += _all_day(f"task-{p['task_id']}-{p['date']}@{host}", d, f"{icon} {title}", zone, stamp)
        elif p["kind"] == "forecast":
            lines += _all_day(
                f"task-{p['task_id']}-km@{host}", d, f"{icon} {title} (prognoza z przebiegu)", zone, stamp
            )
        elif p["kind"] == "done":
            lines += _all_day(f"task-{p['task_id']}-done-{p['date']}@{host}", d, f"✓ {title}", zone, stamp)

    active = {t.id for t in db.scalars(select(Task).where(Task.active.is_(True)))}
    for w in warranties:
        if w.task_id in active or w.item_id not in tree.items or not (start <= w.ends_on <= end):
            continue
        lines += _all_day(f"warranty-{w.id}@{host}", w.ends_on, f"📄 Koniec: {w.name}", tree.label(w.item_id), stamp)

    for occ in schedule.occurrences(db, start, end, user_id=mine, cancelled=False):
        ev = occ.event
        h, m = _hhmm(ev.starts_at)
        begin = datetime(occ.day.year, occ.day.month, occ.day.day, h, m)
        if ev.ends_at:
            eh, em = _hhmm(ev.ends_at)
            finish = datetime(occ.day.year, occ.day.month, occ.day.day, eh, em)
            if finish <= begin:
                finish += timedelta(days=1)
        else:
            finish = begin + timedelta(hours=1)
        where = tree.label(ev.item_id) if ev.item_id in tree.items else ""
        lines += [
            "BEGIN:VEVENT",
            f"UID:sched-{ev.id}-{_day(occ.day)}@{host}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={TZID}:{begin.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID={TZID}:{finish.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{escape('🗓️ ' + ev.title)}",
            *([f"DESCRIPTION:{escape(where)}"] if where else []),
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)
