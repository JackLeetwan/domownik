"""/api/grafik – an outside system keeps its one-off entries in /grafik up to date (e.g. Paula's Zdrofit classes).

Only with `Authorization: Bearer <DOMOWNIK_SYNC_TOKEN>`; without a configured token the API does not exist.

PUT /api/grafik/{feed} sends the feed's whole current list for a window of days. Each entry becomes a one-off
ScheduleEvent (the weekday of its date, starts_on = ends_on = that date) with source "<feed>:<key>":
new keys are added, known ones updated, and upcoming entries of the feed missing from the list are removed
(the class was cancelled). Entries that have already started stay – they are history, and the gym's schedule
no longer shows them as booked.
"""

from __future__ import annotations

import re
import secrets
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import schedule
from domownik.config import get_settings, now
from domownik.db import get_db
from domownik.models import ScheduleEvent, User

router = APIRouter(prefix="/api/grafik")

FEED_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def feeder(request: Request) -> None:
    token = get_settings().sync_token
    if not token:
        raise HTTPException(404)
    given = request.headers.get("authorization", "")
    if not secrets.compare_digest(given.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401)


class Entry(BaseModel):
    key: str = Field(min_length=1, max_length=38)
    title: str = Field(min_length=1, max_length=120)
    date: date
    start: str
    end: str | None = None
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("start", "end")
    @classmethod
    def _time(cls, v: str | None) -> str | None:
        return schedule.parse_time(v)


class Feed(BaseModel):
    assignee: str | None = Field(default=None, max_length=80)  # an account's name; None = shared
    window_from: date
    window_to: date
    entries: list[Entry] = Field(default_factory=list, max_length=200)


def _started(ev: ScheduleEvent, at) -> bool:
    day = ev.starts_on
    return day is not None and (day < at.date() or (day == at.date() and ev.starts_at <= at.strftime("%H:%M")))


@router.put("/{feed}", dependencies=[Depends(feeder)])
def sync(feed: str, body: Feed, db: Session = Depends(get_db)):
    if not FEED_RE.match(feed):
        raise HTTPException(422, "Nazwa kanału: małe litery, cyfry, - i _")
    if body.window_to < body.window_from:
        raise HTTPException(422, "Okno: window_to przed window_from")
    assignee_id = None
    if body.assignee:
        u = db.scalar(select(User).where(User.name == body.assignee, User.is_active.is_(True)))
        if u is None:
            raise HTTPException(422, f"Nie ma domownika {body.assignee!r}")
        assignee_id = u.id

    prefix = f"{feed}:"
    known = {ev.source: ev for ev in db.scalars(select(ScheduleEvent).where(ScheduleEvent.source.startswith(prefix)))}
    added = updated = removed = 0
    wanted = set()
    for e in body.entries:
        src = prefix + e.key
        wanted.add(src)
        ev = known.get(src)
        if ev is None:
            ev = ScheduleEvent(source=src, assign_mode="fixed")
            db.add(ev)
            added += 1
        elif (ev.title, ev.starts_on, ev.starts_at, ev.ends_at, ev.notes, ev.assignee_id) != (
            e.title, e.date, e.start, e.end, e.notes, assignee_id,
        ):  # fmt: skip
            updated += 1
        ev.title, ev.notes = e.title, e.notes
        ev.weekdays = str(e.date.weekday())
        ev.starts_on = ev.ends_on = e.date
        ev.starts_at, ev.ends_at = e.start, e.end
        ev.assignee_id = assignee_id

    at = now().replace(tzinfo=None)
    for src, ev in known.items():
        if src in wanted or ev.starts_on is None:
            continue
        if body.window_from <= ev.starts_on <= body.window_to and not _started(ev, at):
            db.delete(ev)
            removed += 1
    db.commit()
    return {"added": added, "updated": updated, "removed": removed}
