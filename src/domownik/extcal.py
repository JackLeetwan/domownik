"""External calendars: fetch an ICS link, expand its events (recurring ones too) and keep them for the calendar.

Only a window around today is kept (a month back, a bit over a year ahead). A failed fetch keeps the events
from the last good one and shows the error on /kalendarz/zewnetrzne.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

import httpx
import icalendar
from dateutil.rrule import rrulestr
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from domownik.config import get_settings
from domownik.models import ExternalCalendar, ExternalEvent

log = logging.getLogger("domownik")

BACK_DAYS = 31
AHEAD_DAYS = 400
MAX_BYTES = 10 * 1024 * 1024
MAX_EVENTS = 5000
REFRESH_EVERY = timedelta(minutes=60)
TIMEOUT = 20.0


class CalendarError(Exception):
    """Something the user can act on (wrong link, not a calendar, server down) – shown on the page."""


@dataclass(frozen=True)
class Occ:
    start: date | datetime  # local, naive
    end: date | datetime
    all_day: bool
    title: str
    location: str | None


# ---------------------------------------------------------------- fetching


def normalize_url(raw: str) -> str:
    url = raw.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://") :]
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CalendarError("Wklej link zaczynający się od https:// albo webcal://")
    return url


def _check_host(host: str) -> None:
    """Only public servers – the link must not reach into the home network or the server itself."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise CalendarError(f"Nie znam serwera {host} – sprawdź link") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise CalendarError("Ten link prowadzi do sieci lokalnej – Domownik pobiera tylko publiczne kalendarze")


def fetch(url: str) -> bytes:
    def on_request(request: httpx.Request) -> None:
        _check_host(request.url.host)

    try:
        with (
            httpx.Client(
                timeout=TIMEOUT,
                follow_redirects=True,
                max_redirects=5,
                headers={"User-Agent": "Domownik (calendar subscription)"},
                trust_env=False,  # no proxy from the environment – it would bypass the host check
                event_hooks={"request": [on_request]},
            ) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code in (401, 403, 404):
                raise CalendarError(f"Serwer nie oddał kalendarza ({resp.status_code}) – link mógł wygasnąć")
            if resp.status_code >= 400:
                raise CalendarError(f"Serwer kalendarza odpowiedział błędem {resp.status_code}")
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise CalendarError("Kalendarz jest za duży (ponad 10 MB)")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise CalendarError(f"Nie udało się pobrać kalendarza ({type(exc).__name__})") from None


# ---------------------------------------------------------------- parsing


def _local(v: date | datetime) -> date | datetime:
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(get_settings().tz).replace(tzinfo=None)
    return v


def _key(v: date | datetime) -> date | datetime:
    """Comparable key of an occurrence start (RECURRENCE-ID, EXDATE)."""
    return _local(v)


def _dates(prop) -> list[date | datetime]:
    if prop is None:
        return []
    out = []
    for p in prop if isinstance(prop, list) else [prop]:
        out.extend(x.dt for x in getattr(p, "dts", []))
    return out


def _rule_starts(rrule, start: date | datetime, lo: datetime, hi: datetime) -> list[date | datetime]:
    """Occurrence starts of a recurring event inside [lo, hi] (local naive bounds)."""
    text = rrule.to_ical().decode()
    all_day = not isinstance(start, datetime)
    dt = datetime.combine(start, time()) if all_day else start
    if dt.tzinfo is None:
        text = re.sub(r"(UNTIL=\d{8}T\d{6})Z", r"\1", text)  # floating / all-day start: compare UNTIL naively
        bounds = lo, hi
    else:
        text = re.sub(r"UNTIL=(\d{8})(?=;|$)", r"UNTIL=\1T235959Z", text)
        tz = get_settings().tz
        bounds = lo.replace(tzinfo=tz), hi.replace(tzinfo=tz)
    try:
        rule = rrulestr(text, dtstart=dt)
        starts = []
        for s in rule.xafter(bounds[0] - timedelta(days=60), count=MAX_EVENTS, inc=True):
            if s > bounds[1]:
                break
            starts.append(s.date() if all_day else s)
        return starts
    except ValueError, TypeError:
        return [start]


def parse(raw: bytes, today: date) -> list[Occ]:
    """All occurrences overlapping the kept window, sorted."""
    try:
        cal = icalendar.Calendar.from_ical(raw)
    except ValueError:
        raise CalendarError("To nie wygląda na kalendarz ICS – sprawdź, czy link prowadzi do pliku .ics") from None
    if not isinstance(cal, icalendar.Calendar):
        raise CalendarError("To nie wygląda na kalendarz ICS")
    first, last = today - timedelta(days=BACK_DAYS), today + timedelta(days=AHEAD_DAYS)
    lo, hi = datetime.combine(first, time()), datetime.combine(last, time.max)

    events = list(cal.walk("VEVENT"))
    # moved / changed single occurrences of a recurring event: uid → {original start}
    overridden: dict[str, set] = defaultdict(set)
    for ev in events:
        rid = ev.get("RECURRENCE-ID")
        if rid is not None and hasattr(rid, "dt"):
            overridden[str(ev.get("UID", ""))].add(_key(rid.dt))

    out: list[Occ] = []
    for ev in events:
        try:
            out.extend(_occurrences(ev, overridden, first, last, lo, hi))
        except Exception:  # one odd event (date vs datetime, several RRULEs…) never breaks the calendar
            log.warning("external calendar: skipped an event %r", str(ev.get("UID", ""))[:60])
        if len(out) >= MAX_EVENTS:
            del out[MAX_EVENTS:]
            break
    out.sort(key=lambda o: (o.start if isinstance(o.start, datetime) else datetime.combine(o.start, time()), o.title))
    return out


def _occurrences(ev, overridden: dict[str, set], first: date, last: date, lo: datetime, hi: datetime) -> list[Occ]:
    if str(ev.get("STATUS", "")).upper() == "CANCELLED" or ev.get("DTSTART") is None:
        return []
    start = ev.get("DTSTART").dt
    all_day = not isinstance(start, datetime)
    if ev.get("DTEND") is not None:
        end = ev.get("DTEND").dt
        if isinstance(end, datetime) != isinstance(start, datetime):  # mixed date / datetime: use the date
            end = end.date() if isinstance(end, datetime) else datetime.combine(end, time())
        length = end - start
    elif ev.get("DURATION") is not None:
        length = ev.get("DURATION").dt
    else:
        length = timedelta(days=1) if all_day else timedelta(0)
    if isinstance(length, timedelta) and length < timedelta(0):
        length = timedelta(0)
    title = str(ev.get("SUMMARY", "") or "").strip()[:200] or "(bez tytułu)"
    loc = str(ev.get("LOCATION", "") or "").strip()[:200] or None

    rrule = ev.get("RRULE")
    if isinstance(rrule, list):
        rrule = rrule[0] if rrule else None
    if rrule is not None and ev.get("RECURRENCE-ID") is None:
        skip = {_key(x) for x in _dates(ev.get("EXDATE"))} | overridden.get(str(ev.get("UID", "")), set())
        starts = [s for s in _rule_starts(rrule, start, lo, hi) if _key(s) not in skip]
    else:
        starts = [start]
    out = []
    for s in starts:
        ls, le = _local(s), _local(s + length)
        end_day = le if all_day else le.date()
        start_day = ls if all_day else ls.date()
        if end_day < first or start_day > last:
            continue
        out.append(Occ(ls, le, all_day, title, loc))
        if len(out) >= MAX_EVENTS:
            break
    return out


def _rows(occs: list[Occ], calendar_id: int) -> list[ExternalEvent]:
    rows = []
    for o in occs:
        if o.all_day:
            s, e = o.start, o.end - timedelta(days=1)  # DTEND of an all-day event is the day after
            rows.append(
                ExternalEvent(
                    calendar_id=calendar_id, starts_on=s, ends_on=max(s, e), title=o.title, location=o.location
                )
            )
            continue
        s, e = o.start, o.end
        last_day = e.date() if e.time() != time() or e.date() == s.date() else e.date() - timedelta(days=1)
        rows.append(
            ExternalEvent(
                calendar_id=calendar_id,
                starts_on=s.date(),
                ends_on=max(s.date(), last_day),
                starts_at=s.strftime("%H:%M"),
                ends_at=e.strftime("%H:%M") if e > s else None,
                title=o.title,
                location=o.location,
            )
        )
    return rows


# ---------------------------------------------------------------- refreshing


def refresh(db: Session, cal: ExternalCalendar, now: datetime, *, raw: bytes | None = None) -> int:
    """Fetch and replace the calendar's events; returns their number. Errors are stored on the calendar."""
    try:
        occs = parse(raw if raw is not None else fetch(cal.url), now.date())
    except Exception as exc:  # stored on the calendar (and fetched_at, so the scheduler waits an hour)
        if not isinstance(exc, CalendarError):
            log.exception("external calendar %s", cal.id)
            exc = CalendarError("Nie udało się odczytać kalendarza – sprawdź link albo spróbuj później")
        cal.error, cal.fetched_at = str(exc)[:300], now.replace(tzinfo=None)
        db.commit()
        return -1
    db.execute(delete(ExternalEvent).where(ExternalEvent.calendar_id == cal.id))
    db.add_all(_rows(occs, cal.id))
    cal.error, cal.fetched_at = None, now.replace(tzinfo=None)
    db.commit()
    return len(occs)


def refresh_due(db: Session, now: datetime) -> None:
    """Scheduler: calendars not fetched in the last hour (one failing never stops the others)."""
    stale = now.replace(tzinfo=None) - REFRESH_EVERY
    cals = db.scalars(
        select(ExternalCalendar)
        .where(or_(ExternalCalendar.fetched_at.is_(None), ExternalCalendar.fetched_at < stale))
        .execution_options(all_private=True)
    ).all()
    for cal in cals:
        try:
            refresh(db, cal, now)
        except Exception:
            db.rollback()
            log.exception("external calendar %s failed", cal.id)


# ---------------------------------------------------------------- reading


def visible_calendars(db: Session, *, user_id: int | None = None) -> list[ExternalCalendar]:
    """The viewer's calendars (private ones of others are filtered by the session); "Moje": shared + own."""
    q = select(ExternalCalendar).order_by(ExternalCalendar.name)
    if user_id is not None:
        q = q.where(or_(ExternalCalendar.user_id.is_(None), ExternalCalendar.user_id == user_id))
    return list(db.scalars(q))


def events_by_day(
    db: Session, start: date, end: date, *, user_id: int | None = None, calendars: list[ExternalCalendar] | None = None
) -> dict[date, list[ExternalEvent]]:
    """Events per day in [start, end]; a multi-day event is listed on each of its days. All-day ones first."""
    cals = calendars if calendars is not None else visible_calendars(db, user_id=user_id)
    if not cals:
        return {}
    rows = db.scalars(
        select(ExternalEvent).where(
            ExternalEvent.calendar_id.in_([c.id for c in cals]),
            ExternalEvent.starts_on <= end,
            ExternalEvent.ends_on >= start,
        )
    ).all()
    out: dict[date, list[ExternalEvent]] = defaultdict(list)
    for ev in rows:
        d = max(ev.starts_on, start)
        while d <= min(ev.ends_on, end):
            out[d].append(ev)
            d += timedelta(days=1)
    for evs in out.values():
        evs.sort(key=lambda e: (e.starts_at or "", e.title))
    return dict(out)


def for_push(db: Session, day: date, user_id: int) -> list[ExternalEvent]:
    """Events of the day for one person's push: shared calendars and their own (with reminders on)."""
    return for_push_range(db, day, day, user_id).get(day, [])


def for_push_range(db: Session, start: date, end: date, user_id: int) -> dict[date, list[ExternalEvent]]:
    cals = db.scalars(
        select(ExternalCalendar)
        .where(
            ExternalCalendar.notify.is_(True),
            or_(ExternalCalendar.user_id.is_(None), ExternalCalendar.user_id == user_id),
            or_(ExternalCalendar.private_to_id.is_(None), ExternalCalendar.private_to_id == user_id),
        )
        .execution_options(all_private=True)
    ).all()
    return events_by_day(db, start, end, calendars=list(cals))
