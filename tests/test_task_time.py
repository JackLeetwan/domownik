"""Hour of a task (appointments): form, calendar, ICS, digest text and the push before the hour."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta

import icalendar
import pytest
from sqlalchemy import select

from domownik import ics, notify, services, webpush
from domownik.config import today
from domownik.domain.tree import Tree
from domownik.models import Item, NotificationLog, PushSubscription, Task, User
from domownik.security import hash_password

QUICK = {"HX-Request": "true", "X-Quick-Add": "1"}


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []

    def fake_deliver(sub, data, *, urgent=False):
        out.append(json.loads(data) | {"_endpoint": sub.endpoint})
        return True

    monkeypatch.setattr(webpush, "deliver", fake_deliver)
    return out


def _item(db, name="Dom", kind="house") -> Item:
    it = Item(name=name, kind=kind)
    db.add(it)
    db.commit()
    return it


def _device(db, user, endpoint="https://push.example.com/a"):
    db.add(PushSubscription(user_id=user.id, endpoint=endpoint, p256dh="pk", auth="au"))
    db.commit()


def _timed(db, item, title, day, hhmm, *, remind=60, ends=None, **kw) -> Task:
    t = Task(
        item_id=item.id,
        title=title,
        category="event",
        recurrence={"mode": "once"},
        next_due_date=day,
        due_time=hhmm,
        ends_at=ends,
        remind_minutes=remind,
        lead_days=1,
        direction="none",
        **kw,
    )
    db.add(t)
    db.commit()
    return t


def test_quick_event_saves_hour_and_reminder(auth_client, db, user):
    item = _item(db)
    form = auth_client.get("/dodaj/event", headers={"HX-Request": "true"}).text
    assert 'name="due_time"' in form and "dzień wcześniej" in form
    r = auth_client.post(
        "/tasks/new",
        data={
            "title": "Dentysta",
            "item_id": item.id,
            "freq": "once",
            "category": "event",
            "direction": "none",
            "lead_days": "1",
            "next_due_date": "2030-05-06",
            "due_time": "14:30",
            "ends_at": "15:15",
            "remind_minutes": "30",
            "assignee_id": user.id,
        },
        headers=QUICK,
    )
    assert r.status_code == 200, r.text
    t = db.scalar(select(Task).where(Task.title == "Dentysta"))
    assert (t.due_time, t.ends_at, t.remind_minutes, t.category) == ("14:30", "15:15", 30, "event")
    assert t.time_label == "14:30–15:15" and t.direction == "none"


def test_end_before_start_and_bad_hour_are_messages(auth_client, db):
    item = _item(db)
    base = {"title": "X", "item_id": item.id, "freq": "once", "next_due_date": "2030-05-06"}
    r = auth_client.post("/tasks/new", data=base | {"due_time": "15:00", "ends_at": "14:00"}, headers=QUICK)
    assert "Godzina końca" in r.text
    r = auth_client.post("/tasks/new", data=base | {"due_time": "25:00"}, headers=QUICK)
    assert "GG:MM" in r.text
    assert db.scalar(select(Task)) is None


def test_no_hour_means_no_reminder(auth_client, db):
    item = _item(db)
    data = {"title": "Bez godziny", "item_id": item.id, "freq": "once", "next_due_date": "2030-05-06"}
    auth_client.post("/tasks/new", data=data | {"remind_minutes": "60", "ends_at": "10:00"}, headers=QUICK)
    t = db.scalar(select(Task))
    assert (t.due_time, t.ends_at, t.remind_minutes, t.time_label) == (None, None, None, None)


def test_full_form_shows_and_keeps_hour(auth_client, db):
    item = _item(db)
    t = _timed(db, item, "Zebranie", today() + timedelta(days=3), "18:00", remind=1440)
    page = auth_client.get(f"/tasks/{t.id}/edit").text
    assert 'value="18:00"' in page and '<option value="1440" selected>' in page


def test_calendar_orders_day_by_hour(db):
    item = _item(db)
    d = today() + timedelta(days=2)
    _timed(db, item, "Późno", d, "18:00")
    _timed(db, item, "Rano", d, "08:15")
    tree = Tree.load(db)
    entries = services.calendar_day(db, tree, today(), d)
    assert [e["title"] for e in entries] == ["Rano", "Późno"]
    assert entries[0]["time"] == "08:15"


def test_ics_has_timed_event(db, user):
    item = _item(db)
    d = today() + timedelta(days=2)
    _timed(db, item, "Dentysta", d, "14:30", ends="15:15")
    _timed(db, item, "Bez końca", d, "09:00")
    cal = icalendar.Calendar.from_ical(ics.build(db, user, today(), datetime.now()))
    ev = {str(e["SUMMARY"]): e for e in cal.walk("VEVENT")}
    a = ev["📅 Dentysta"]
    assert a["DTSTART"].dt.hour == 14 and a["DTSTART"].dt.minute == 30
    assert a["DTEND"].dt.hour == 15 and a["DTEND"].dt.minute == 15
    b = ev["📅 Bez końca"]
    assert b["DTEND"].dt - b["DTSTART"].dt == timedelta(hours=1)


def test_digest_line_has_hour(db, user):
    _device(db, user)
    _timed(db, _item(db), "Dentysta", today(), "14:30")
    (d,) = notify.build_digests(db, "morning", today())
    assert "📅 Dziś: 14:30 Dentysta" in d.message()


def test_push_before_the_hour_once(db, user, sent):
    _device(db, user)
    d = today() + timedelta(days=1)
    _timed(db, _item(db), "Dentysta", d, "14:30", remind=60)
    at = datetime.combine(d, time(13, 0))
    assert notify.run_timed(db, at) == 0  # too early
    assert notify.run_timed(db, datetime.combine(d, time(13, 30))) == 1
    assert sent[0]["title"] == "⏰ 14:30 Dentysta"
    assert sent[0]["body"].startswith("Za 1 godz.") and sent[0]["actions"][0]["url"].startswith("/n/done/")
    assert notify.run_timed(db, datetime.combine(d, time(13, 45))) == 0  # already sent
    assert db.scalar(select(NotificationLog.kind)) == "at_time"


def test_push_goes_to_assignee_only_and_not_after_grace(db, user, sent):
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("x" * 10))
    db.add(other)
    db.commit()
    _device(db, user)
    _device(db, other, "https://push.example.com/b")
    item = _item(db)
    d = today() + timedelta(days=1)
    _timed(db, item, "Moje", d, "10:00", remind=0, assignee_id=other.id)
    _timed(db, item, "Dawno", d, "08:00", remind=0)
    assert notify.run_timed(db, datetime.combine(d, time(10, 5))) == 1
    assert [s["_endpoint"] for s in sent] == ["https://push.example.com/b"]
    assert sent[0]["body"].startswith("Teraz")


def test_tick_sends_timed_push(db, user, sent):
    _device(db, user)
    d = today() + timedelta(days=1)
    _timed(db, _item(db), "Zebranie", d, "21:00", remind=1440)
    notify.tick(db, datetime.combine(today(), time(21, 10)))
    assert any(s["title"] == "⏰ 21:00 Zebranie" for s in sent)
    assert any(s["body"].startswith("Jutro o 21:00") for s in sent)


def test_migration_0015_up_and_down(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn:
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(task)"))}
            assert {"due_time", "ends_at", "remind_minutes"} <= cols
            command.downgrade(cfg, "9a4d3c7b5e21")
            cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(task)"))}
            assert not cols & {"due_time", "ends_at", "remind_minutes"}
            command.upgrade(cfg, "head")
