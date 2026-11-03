"""Calendar subscription: token (hash only, revocable), iCalendar format, no amounts, range, "mine" filter."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import icalendar
from sqlalchemy import select

from domownik import ics
from domownik.config import today
from domownik.models import Item, ScheduleEvent, Task, User, Warranty
from domownik.security import hash_password


def _link(auth_client) -> str:
    r = auth_client.post("/ustawienia/kalendarz/nowy", follow_redirects=True)
    assert r.status_code == 200
    m = re.search(r'data-copy="(http[^"]+/ics/([A-Za-z0-9_-]+)\.ics)"', r.text)
    assert m, "link not shown after creating it"
    return m.group(1).split("testserver")[1]


def _parse(text: str) -> icalendar.Calendar:
    cal = icalendar.Calendar.from_ical(text)
    assert cal.get("VERSION") == "2.0" and cal.get("PRODID")
    return cal


def _events(cal) -> dict[str, icalendar.Event]:
    return {str(e["UID"]): e for e in cal.walk("VEVENT")}


def _setup(db, user):
    t = today()
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("x" * 12))
    car = Item(name="Auto 1", kind="car")
    db.add_all([other, car])
    db.flush()
    db.add_all(
        [
            Task(
                item_id=car.id,
                title="OC; polisa, raty\\nowe",
                category="insurance",
                recurrence={"mode": "once"},
                next_due_date=t + timedelta(days=10),
                amount_gr=123456,
                direction="expense",
            ),
            Task(
                item_id=car.id,
                title="Opłata",
                category="payment",
                recurrence={"mode": "monthly", "day": t.day if t.day <= 28 else 28},
                next_due_date=t + timedelta(days=3),
                amount_gr=9900,
                direction="expense",
                assignee_id=other.id,
            ),
            Task(
                item_id=car.id,
                title="Daleko",
                recurrence={"mode": "once"},
                next_due_date=t + timedelta(days=400),
                direction="none",
            ),
            ScheduleEvent(
                title="Basen",
                weekdays="0,1,2,3,4,5,6",
                starts_at="17:00",
                ends_at="18:30",
                item_id=car.id,
                assignee_id=user.id,
                day_assignees={},
            ),
            ScheduleEvent(title="Odbiór", weekdays="0", starts_at="16:00", assignee_id=other.id, day_assignees={}),
            Warranty(item_id=car.id, name="Gwarancja silnika", ends_on=t + timedelta(days=50)),
        ]
    )
    db.commit()
    return car, other


def test_feed_format_escaping_no_amounts_and_range(auth_client, db, user):
    _setup(db, user)
    url = _link(auth_client)
    anon = auth_client.__class__(auth_client.app)  # a calendar app: no session cookie
    r = anon.get(url)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "\r\n" in r.text and all(len(line.encode()) <= 75 for line in r.text.split("\r\n"))
    for amount in ("123456", "1234,56", "1 234", "9900", "99,00", "zł", "PLN"):
        assert amount not in r.text
    assert "OC\\; polisa\\, raty\\\\nowe" in r.text  # RFC 5545 escaping
    cal = _parse(r.text)
    events = _events(cal)
    t = today()
    oc = next(e for e in events.values() if "OC" in str(e["SUMMARY"]))
    assert str(oc["SUMMARY"]) == "🛡️ OC; polisa, raty\\nowe"
    assert oc["DTSTART"].dt == t + timedelta(days=10)  # all-day: a date, not a datetime
    assert oc["DTEND"].dt == t + timedelta(days=11)
    assert "DTSTAMP" in oc and str(oc["DESCRIPTION"]) == "Auto 1"
    # recurring task: several occurrences, each with its own stable UID
    pay = [uid for uid, e in events.items() if str(e["SUMMARY"]).endswith("Opłata")]
    assert len(pay) >= 12 and len(set(pay)) == len(pay)
    assert not any("Daleko" in str(e["SUMMARY"]) for e in events.values())  # beyond +365 days
    # schedule with time in Europe/Warsaw
    swim = [e for e in events.values() if "Basen" in str(e["SUMMARY"])]
    assert 300 < len(swim) <= 396
    assert swim[0]["DTSTART"].params["TZID"] == "Europe/Warsaw"
    assert (swim[0]["DTEND"].dt - swim[0]["DTSTART"].dt) == timedelta(minutes=90)
    assert cal.walk("VTIMEZONE")
    # warranty without a reminder task gets its own event
    assert any(uid.startswith("warranty-") for uid in events)
    # stable UIDs between two downloads
    assert set(_events(_parse(anon.get(url).text))) == set(events)


def test_mine_filter(auth_client, db, user):
    _setup(db, user)
    url = _link(auth_client)
    auth_client.post("/ustawienia/kalendarz/zakres", data={"scope": "mine"})
    text = auth_client.__class__(auth_client.app).get(url).text
    summaries = [str(e["SUMMARY"]) for e in _parse(text).walk("VEVENT")]
    assert any("OC" in s for s in summaries)  # shared
    assert not any(s.endswith("Opłata") for s in summaries)  # someone else's
    assert any("Basen" in s for s in summaries) and not any("Odbiór" in s for s in summaries)
    db.expire_all()
    assert db.get(User, user.id).ics_scope == "mine"


def test_bad_and_revoked_token_404(auth_client, db, user):
    url = _link(auth_client)
    anon = auth_client.__class__(auth_client.app)
    assert anon.get(url).status_code == 200
    assert anon.get("/ics/" + "a" * 43 + ".ics").status_code == 404
    assert anon.get("/ics/short.ics").status_code == 404
    new_url = _link(auth_client)  # "wygeneruj nowy link" invalidates the old one
    assert new_url != url
    assert anon.get(url).status_code == 404
    assert anon.get(new_url).status_code == 200
    auth_client.post("/ustawienia/kalendarz/wylacz")
    assert anon.get(new_url).status_code == 404


def test_only_hash_is_stored(auth_client, db, user):
    url = _link(auth_client)
    token = url.rsplit("/", 1)[1].removesuffix(".ics")
    db.expire_all()
    stored = db.get(User, user.id).ics_token_hash
    assert stored == ics.token_hash(token) and token not in stored and len(stored) == 64
    page = auth_client.get("/ustawienia").text  # shown once only
    assert token not in page and "Link do kalendarza jest aktywny" in page


def test_inactive_user_feed_404(auth_client, db, user):
    url = _link(auth_client)
    db.get(User, user.id).is_active = False
    db.commit()
    assert auth_client.__class__(auth_client.app).get(url).status_code == 404


def test_fold_and_escape():
    line = "SUMMARY:" + "ż" * 60
    folded = ics.fold(line)
    parts = folded.split("\r\n ")
    assert all(len(p.encode()) <= 75 for p in parts) and "".join(parts) == line
    assert ics.escape("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"


def test_access_log_redacts_token():
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/ics/SeCrEt-123.ics", "1.1", 200),
        None,
    )
    ics.RedactIcsToken().filter(rec)
    assert "SeCrEt" not in rec.getMessage() and "/ics/…" in rec.getMessage()
    ics.install_log_filter()
    ics.install_log_filter()
    assert sum(isinstance(f, ics.RedactIcsToken) for f in logging.getLogger("uvicorn.access").filters) == 1


def test_no_events_without_data(db, user):
    text = ics.build(db, user, today(), datetime.now())
    cal = _parse(text)
    assert cal.walk("VEVENT") == []
    assert db.scalar(select(User.ics_scope)) == "all"


def test_migration_0014_up_and_down(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "2f8b6d4e1a93")
            conn.execute(
                sa.text("INSERT INTO user (id, name, email, password_hash, is_active) VALUES (1, 'A', 'a@x', 'h', 1)")
            )
            command.upgrade(cfg, "9a4d3c7b5e21")
            assert conn.execute(sa.text("SELECT ics_scope, ics_token_hash FROM user")).one() == ("all", None)
            conn.execute(sa.text("UPDATE user SET ics_token_hash = 'x'"))
            command.downgrade(cfg, "2f8b6d4e1a93")
            cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(user)"))}
            assert not cols & {"ics_scope", "ics_token_hash"}
            assert conn.execute(sa.text("SELECT count(*) FROM user")).scalar() == 1
            command.upgrade(cfg, "head")
