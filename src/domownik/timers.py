"""Timers ("⏱ Pranie – 2 h"): a countdown that ends with a push.

When a timer ends, the person who started it gets a push (nobody with a device among them: everyone with one).
Nobody tapped "Zrobione" or "+15 min" for 15 minutes → one more push, this time to the whole household, so
whoever is at home can move the laundry. The minute job in main.py calls run(); a server that was down
catches up on a timer that ended less than CATCH_UP ago and drops older ones quietly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import services, webpush
from domownik.config import get_settings, now
from domownik.models import PushSubscription, Timer, User

log = logging.getLogger("domownik.timers")

PRESETS = (60, 120, 180)  # the three buttons of the quick-add form
DEFAULT_TITLE = "Pranie"
MAX_MINUTES = 24 * 60
EXTEND = timedelta(minutes=15)  # "+15 min"
RENOTIFY_AFTER = timedelta(minutes=15)  # nobody reacted → the whole household
CATCH_UP = timedelta(hours=3)  # a server back after a longer break does not push about old laundry
AUTO_CLOSE = timedelta(hours=12)  # a timer nobody closed leaves the dashboard by itself
TOKEN_MAX_AGE = 2 * 86400


def _now() -> datetime:
    return now().replace(tzinfo=None)


def duration_text(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m} min"
    return f"{h} godz." + (f" {m} min" if m else "")


def start(db: Session, user: User, title: str, minutes: int, at: datetime | None = None) -> Timer:
    at = at or _now()
    t = Timer(
        title=(title.strip() or DEFAULT_TITLE)[:80],
        user_id=user.id,
        minutes=minutes,
        ends_at=at.replace(microsecond=0) + timedelta(minutes=minutes),
    )
    db.add(t)
    db.commit()
    return t


def active(db: Session) -> list[Timer]:
    return list(db.scalars(select(Timer).where(Timer.finished_at.is_(None)).order_by(Timer.ends_at, Timer.id)))


def recent_titles(db: Session, limit: int = 6) -> list[str]:
    """Titles used before, most recent first – suggestions of the form ("Pranie", "Zmywarka"…)."""
    out: list[str] = []
    for title in db.scalars(select(Timer.title).order_by(Timer.id.desc()).limit(50)):
        if title not in out:
            out.append(title)
        if len(out) == limit:
            break
    return out


def extend(db: Session, t: Timer, at: datetime | None = None) -> None:
    """+15 min from now (or from the end, when it has not ended yet); the pushes start over."""
    at = at or _now()
    t.ends_at = max(t.ends_at, at.replace(microsecond=0)) + EXTEND
    t.notified_at = t.renotified_at = None
    db.commit()


def finish(db: Session, t: Timer, at: datetime | None = None) -> None:
    if t.finished_at is None:
        t.finished_at = at or _now()
        db.commit()


# ---------------------------------------------------------------- push


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().resolved_secret_key(), salt="push-timer")


def token(t: Timer) -> str:
    return _serializer().dumps({"i": t.id})


def read_token(value: str) -> int | None:
    try:
        return int(_serializer().loads(value, max_age=TOKEN_MAX_AGE)["i"])
    except BadSignature, SignatureExpired, KeyError, TypeError, ValueError:
        return None


def payload(t: Timer, *, again: bool) -> dict[str, Any]:
    tok = token(t)
    who = f" · {t.user.name}" if t.user else ""
    body = (
        "Czeka już 15 min – kto jest w domu?" + who
        if again
        else f"Minęło {duration_text(t.minutes)} (start {t.ends_at - timedelta(minutes=t.minutes):%H:%M})."
    )
    return {
        "title": f"⏱ {t.title} – koniec",
        "body": body,
        "url": "/",
        "tag": f"domownik-timer-{t.id}",
        "urgent": True,
        "actions": [
            {"action": "timer-done", "title": "✓ Zrobione", "url": f"/n/timer/{tok}/done"},
            {"action": "timer-plus", "title": "+15 min", "url": f"/n/timer/{tok}/plus"},
        ],
    }


def _send_to(db: Session, users: list[User], body: dict[str, Any]) -> int:
    from domownik import notify

    sent = 0
    for user in users:
        try:
            if notify.send(db, user, body):
                sent += 1
        except webpush.PushError as exc:
            log.warning("timer push: %s – %s", user.name, exc)
    return sent


def run(db: Session, at: datetime | None = None) -> int:
    """Scheduler step (every minute). Returns how many pushes went out."""
    at = (at or _now()).replace(tzinfo=None)
    sent = 0
    for t in active(db):
        if t.ends_at > at:
            break  # ordered by ends_at: the rest are still running
        if at - t.ends_at >= AUTO_CLOSE:
            t.finished_at = at
            db.commit()
            continue
        with_device = set(db.scalars(select(PushSubscription.user_id)))
        household = [u for u in services.active_users(db) if u.id in with_device]
        if t.notified_at is None:
            t.notified_at = at  # first, so a failing push service does not repeat it every minute
            db.commit()
            if at - t.ends_at > CATCH_UP:
                continue
            starter = [u for u in household if u.id == t.user_id]
            sent += _send_to(db, starter or household, payload(t, again=False))
        elif t.renotified_at is None and at - t.notified_at >= RENOTIFY_AFTER:
            t.renotified_at = at
            db.commit()
            sent += _send_to(db, household, payload(t, again=True))
    return sent
