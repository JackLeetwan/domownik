"""Unhandled errors: nothing a user, a date or a broken file can do ends in a 500 or a dead scheduler.

- the whole app crawled on awkward dates (month ends, 29 February, DST changes, New Year) – pages,
  digests, the scheduler tick and the ICS feed;
- the recurrence engine over thousands of random rules and dates;
- edge values that pass the forms (year 0001 / 9999, huge amounts, 23:59) never break a page later;
- a failing scheduler step or one person's broken digest does not hold back the rest;
- bank statements and uploads that are not what they claim, files gone from disk, accounts switched
  off, hand-typed URLs, and a real bug – each gets a readable answer.
"""

from __future__ import annotations

import io
import random
import re
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from domownik import config, documents, ics, notify, services, webpush
from domownik.config import get_settings, today
from domownik.domain import recurrence as R
from domownik.models import (
    BoardNote,
    Comment,
    Item,
    PushSubscription,
    RecurringIncome,
    ScheduleEvent,
    Task,
    User,
)
from domownik.security import hash_password
from tests.test_security import _routes, _seed

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")


def _client(auth_client) -> TestClient:
    """The logged-in client, but a server error comes back as a response instead of an exception."""
    c = TestClient(auth_client.app, raise_server_exceptions=False, cookies=auth_client.cookies)
    c.headers.update(auth_client.headers)
    return c


def _jpeg(size=(60, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (20, 120, 60)).save(buf, "JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------- the clock


class Clock:
    """Moves "now" for the whole app: config.now and every module that imported it by name."""

    def __init__(self, monkeypatch):
        self.at: datetime | None = None
        real = config.now

        def fake():
            return self.at or real()

        monkeypatch.setattr(config, "now", fake)
        for name, mod in list(sys.modules.items()):
            if name.startswith("domownik") and getattr(mod, "now", None) is real:
                monkeypatch.setattr(mod, "now", fake)

    def set(self, d: date, hour: int = 12) -> datetime:
        self.at = datetime(d.year, d.month, d.day, hour, tzinfo=get_settings().tz)
        return self.at


AWKWARD_DAYS = [
    date(2026, 11, 30),
    date(2026, 12, 31),
    date(2027, 1, 1),
    date(2027, 1, 31),
    date(2027, 2, 28),
    date(2027, 3, 1),
    date(2027, 3, 27),  # the night before summer time
    date(2027, 3, 28),  # summer time starts
    date(2027, 4, 30),
    date(2027, 10, 31),  # winter time
    date(2027, 12, 31),
    date(2028, 2, 28),
    date(2028, 2, 29),
    date(2028, 3, 1),
]


def _tricky_tasks(db, house: Item) -> None:
    base = {"item_id": house.id, "category": "payment", "amount_gr": 12345, "direction": "expense"}
    rules = [
        ({"mode": "monthly", "day": 31}, date(2026, 11, 30)),
        ({"mode": "fixed", "dates": ["02-29"]}, date(2028, 2, 29)),
        ({"mode": "easter", "offset": -46}, date(2027, 2, 10)),
        ({"mode": "nth_weekday", "month": 10, "weekday": "sun", "n": -1}, date(2027, 10, 31)),
        ({"mode": "interval", "every": 1, "unit": "month", "anchor": "due"}, date(2027, 1, 31)),
        ({"mode": "interval", "every": 2, "unit": "week", "anchor": "completion", "count": 3, "done": 2}, today()),
        ({"mode": "monthly", "day": 15, "until": "2027-06-30"}, date(2026, 12, 15)),
    ]
    for i, (rule, due) in enumerate(rules):
        db.add(Task(title=f"Reguła {i}", recurrence=R.validate(rule), next_due_date=due, **base))
    db.add(
        Task(
            title="Wizyta o północy",
            item_id=house.id,
            category="health",
            recurrence={"mode": "once"},
            next_due_date=date(2027, 3, 28),
            due_time="02:30",  # does not exist in Warsaw that night
            ends_at="00:15",
            remind_minutes=60,
        )
    )
    db.add(ScheduleEvent(title="Basen", weekdays="0,6", starts_at="23:30", ends_at="00:30"))
    db.add(
        RecurringIncome(name="Najem", amount_gr=150000, day=31, starts_on=date(2026, 11, 1), ends_on=date(2027, 2, 28))
    )
    db.commit()


def test_whole_app_on_awkward_dates(auth_client, db, user, monkeypatch):
    _seed(db, user)
    db.add(PushSubscription(user_id=user.id, endpoint="https://fcm.googleapis.com/fcm/send/x", p256dh="k", auth="a"))
    _tricky_tasks(db, db.scalar(select(Item).where(Item.kind == "house")))
    sent = []
    monkeypatch.setattr(webpush, "deliver", lambda sub, data, urgent=False: sent.append(data) or True)
    clock = Clock(monkeypatch)
    c = _client(auth_client)
    pages = [p for m, p, _ in _routes(c.app) if m == "GET" and "{" not in p and p not in ("/logout", "/sw.js")]
    pages += [f"/items/{i}" for i in db.scalars(select(Item.id))]
    pages += [f"/tasks/{i}" for i in db.scalars(select(Task.id))]
    errors = []
    for d in AWKWARD_DAYS:
        for hour in (8, 19, 23):
            now = clock.set(d, hour)
            try:
                notify.tick(db, now)
                ics.build(db, user, d, now)
            except Exception as exc:
                errors.append(f"{d} {hour}h background: {exc!r}")
        clock.set(d)
        for p in pages:
            for headers in ({}, HX):
                r = c.get(p, params={"d": d.isoformat(), "m": d.strftime("%Y-%m")}, headers=headers)
                if r.status_code >= 500:
                    errors.append(f"{d} GET {p}: {r.status_code}")
        r = c.get("/api/timeline", params={"start": (d - timedelta(days=90)).isoformat(), "end": f"{d.year + 2}-01-01"})
        if r.status_code != 200:
            errors.append(f"{d} timeline: {r.status_code}")
    assert errors == []
    assert sent  # the scheduler did run (digests / reminders went out)


# ---------------------------------------------------------------- the recurrence engine


def _random_rule(rng: random.Random) -> dict:
    mode = rng.choice(R.MODES)
    rule: dict = {"mode": mode}
    if mode == "interval":
        rule |= {
            "every": rng.choice([1, 2, 3, 6, 12, 52, 365, 999]),
            "unit": rng.choice(R.UNITS),
            "anchor": rng.choice(["due", "completion"]),
        }
    elif mode == "monthly":
        rule["day"] = rng.choice([1, 15, 28, 29, 30, 31])
    elif mode == "fixed":
        rule["dates"] = rng.sample(["01-01", "02-28", "02-29", "03-31", "04-30", "12-31"], rng.randint(1, 3))
    elif mode == "easter":
        rule["offset"] = rng.choice([-366, -46, 0, 39, 60, 365])
    elif mode == "nth_weekday":
        rule |= {"month": rng.randint(1, 12), "weekday": rng.choice(list(R.WEEKDAYS)), "n": rng.choice([1, 4, 5, -1])}
    if mode != "once" and rng.random() < 0.3:
        if rng.random() < 0.5:
            rule["count"] = rng.randint(1, 12)
            rule["done"] = rng.randint(0, rule["count"])
        else:
            rule["until"] = (date(2000, 1, 1) + timedelta(days=rng.randrange(40000))).isoformat()
    return rule


def test_recurrence_engine_never_crashes():
    rng = random.Random(2026)

    def day() -> date:
        return date(1990, 1, 1) + timedelta(days=rng.randrange(40000))

    checked = 0
    for _ in range(4000):
        try:
            rule = R.validate(_random_rule(rng))
        except R.RuleError:
            continue  # refused up front with a message – that is the handled path
        t, prev, done = day(), day(), day()
        first = R.first_due(rule, t)
        nxt = R.next_due(rule, previous_due=prev, done_on=done)
        R.occurrences(rule, first or t, t - timedelta(days=40), t + timedelta(days=400))
        moved, _ = R.advance(rule, previous_due=prev, done_on=done)
        R.retreat(moved)
        R.postpone(rule, current_due=prev)
        R.series_position(rule)
        R.describe(rule)
        if R.is_calendar(rule) and nxt is not None:
            assert nxt > prev, rule
        checked += 1
    assert checked > 3000


def test_fifth_weekday_of_february_is_refused():
    with pytest.raises(R.RuleError, match="Luty"):
        R.validate({"mode": "nth_weekday", "month": 2, "weekday": "thu", "n": 5})
    assert R.validate({"mode": "nth_weekday", "month": 2, "weekday": "thu", "n": -1})


# ---------------------------------------------------------------- edge values typed into forms


def test_edge_values_never_break_pages_later(auth_client, db, user):
    """What the browser lets through (year 0001 or 9999, huge amounts, midnight) is either refused with a
    message or stored – and then every page still opens."""
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.commit()
    c = _client(auth_client)
    statuses = []
    for when in ("0001-01-01", "9999-12-31", "2028-02-29"):
        for amount in ("0", "999999999", "0,01", "-5"):  # 999999999 is refused with a message
            statuses.append(
                c.post(
                    "/tasks/new",
                    data={
                        "title": f"Skrajne {when} {amount}",
                        "item_id": house.id,
                        "category": "payment",
                        "mode": "interval",
                        "every": "999",
                        "unit": "year",
                        "next_due_date": when,
                        "due_time": "00:00",
                        "ends_at": "23:59",
                        "amount": amount,
                        "direction": "expense",
                        "lead_days": "99999",
                    },
                ).status_code
            )
        statuses.append(
            c.post("/budzet/wpis", data={"amount": "999999999", "booked_on": when, "note": "x"}).status_code
        )
        statuses.append(c.post("/do-zrobienia", data={"title": "Coś", "due_on": when}).status_code)
        statuses.append(c.post("/notatki", data={"body": "x" * 20000, "valid_until": when}).status_code)
        statuses.append(c.post("/zakupy", data={"text": "😀‍👩‍👩‍👧 " * 300}).status_code)
    assert all(s < 500 for s in statuses), statuses
    assert db.scalar(select(Task).where(Task.title.like("Skrajne%"))) is not None  # some did get stored

    pages = [p for m, p, _ in _routes(c.app) if m == "GET" and "{" not in p and p not in ("/logout", "/sw.js")]
    pages += [f"/tasks/{i}" for i in db.scalars(select(Task.id))] + [f"/items/{house.id}"]
    bad = [p for p in pages for h in ({}, HX) if c.get(p, headers=h).status_code >= 500]
    assert bad == []
    for kind in ("morning", "evening"):
        notify.build_digests(db, kind, today())
    ics.build(db, user, today(), config.now())


# ---------------------------------------------------------------- the scheduler keeps going


def test_one_failing_step_does_not_stop_the_tick(db, user, monkeypatch):
    db.add(Item(name="Dom", kind="house"))
    db.commit()
    ran = []

    def broken(*a, **k):
        raise RuntimeError("bad row")

    monkeypatch.setattr(services, "settle_autopay", broken)
    monkeypatch.setattr(notify, "run_slot", lambda db, slot, d: ran.append(slot))
    monkeypatch.setattr(notify, "run_timed", lambda db, now: ran.append("timed"))
    now = datetime.combine(today(), datetime.min.time()).replace(hour=9, tzinfo=get_settings().tz)
    notify.tick(db, now)
    assert ran == ["morning", "timed"]


def test_one_broken_digest_does_not_stop_the_others(db, user, monkeypatch):
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    house = Item(name="Dom", kind="house")
    db.add_all([other, house])
    db.commit()
    db.add(
        Task(
            title="Czynsz",
            item_id=house.id,
            category="payment",
            recurrence={"mode": "monthly", "day": 10},
            next_due_date=today(),
        )
    )
    for u in (user, other):
        db.add(
            PushSubscription(user_id=u.id, endpoint=f"https://fcm.googleapis.com/fcm/send/{u.id}", p256dh="k", auth="a")
        )
    db.commit()
    got = []
    real_payload = notify.payload

    def payload(d):
        if d.user.id == user.id:
            raise RuntimeError("bug in one digest")
        return real_payload(d)

    monkeypatch.setattr(notify, "payload", payload)
    monkeypatch.setattr(webpush, "deliver", lambda sub, data, urgent=False: got.append(sub.user_id) or True)
    assert notify.run_slot(db, "morning", today()) == 1
    assert got == [other.id]


# ---------------------------------------------------------------- files that are not what they say


def test_bank_statement_that_is_not_a_csv(auth_client):
    c = _client(auth_client)
    for name, raw in (
        ("zdjecie.csv", _jpeg()),
        ("puste.csv", b""),
        ("cudzyslow.csv", b'Data;Kwota\n2026-11-01;"12,50\n2026-11-02;3'),
        ("excel.csv", b"PK\x03\x04" + b"\x00" * 200),
        ("smieci.csv", bytes(range(256)) * 20),
    ):
        r = c.post("/budzet/import", files={"file": (name, raw, "text/csv")})
        assert r.status_code < 500, name
        assert "CSV" in r.text or "nagłówk" in r.text, name


def test_broken_and_hostile_uploads(auth_client, db):
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.commit()
    c = _client(auth_client)
    jpg = _jpeg()
    bomb = io.BytesIO()
    Image.new("1", (40000, 40000)).save(bomb, "PNG")  # 200 kB on disk, 1.6 GB of pixels
    for name, raw in (("ucięte.jpg", jpg[: len(jpg) // 2]), ("puste.jpg", b""), ("bomba.png", bomb.getvalue())):
        for url, data in ((f"/items/{house.id}/files", {}), ("/notatki", {"body": "x"})):
            r = c.post(url, data=data, files={"files": (name, raw, "image/jpeg")}, headers=HX)
            assert 400 <= r.status_code < 500, (url, name, r.status_code)
    assert not any((get_settings().uploads_dir / "files").rglob("*.*"))  # nothing half-saved


def test_file_gone_from_disk(auth_client, db, user):
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.flush()
    (att,) = documents.store_files(db, house, [("plan.jpg", io.BytesIO(_jpeg()))], user=user)
    db.commit()
    documents.file_path(att).unlink()
    documents.thumb_path(att).unlink(missing_ok=True)
    c = _client(auth_client)
    assert c.get(f"/files/{att.id}").status_code == 404
    assert c.get(f"/files/{att.id}/thumb").status_code == 404
    assert c.get(f"/items/{house.id}").status_code == 200
    assert c.post(f"/files/{att.id}/delete").status_code < 500


# ---------------------------------------------------------------- people and addresses


def test_switched_off_account_leaves_no_broken_pages(auth_client, db, user):
    """Someone's account is switched off: their tasks, notes and comments stay and every page still opens."""
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    house = Item(name="Dom", kind="house")
    db.add_all([other, house])
    db.flush()
    db.add(Item(name="Ola", kind="member", parent_id=house.id, user_id=other.id))
    t = Task(
        title="Odebrać paczkę",
        item_id=house.id,
        category="other",
        recurrence={"mode": "once"},
        next_due_date=today(),
        assignee_id=other.id,
    )
    note = BoardNote(body="Od Oli", created_by_id=other.id)
    db.add_all([t, note])
    db.flush()
    db.add_all(
        [Comment(task_id=t.id, user_id=other.id, body="Zrobię"), Comment(note_id=note.id, user_id=other.id, body="!")]
    )
    other.is_active = False
    db.commit()
    c = _client(auth_client)
    pages = [p for m, p, _ in _routes(c.app) if m == "GET" and "{" not in p and p not in ("/logout", "/sw.js")]
    pages += [f"/tasks/{t.id}", f"/items/{house.id}"]
    assert [p for p in pages if c.get(p).status_code >= 500] == []
    for kind in ("morning", "evening"):
        notify.build_digests(db, kind, today())


def test_hand_typed_urls_get_a_page_not_json(auth_client):
    c = _client(auth_client)
    for url, code in (("/items/abc", 404), ("/tasks/1.5", 404), ("/kalendarz?d=abc", 400), ("/kalendarz/dzien", 400)):
        r = c.get(url)
        assert r.status_code == code and r.headers["content-type"].startswith("text/html"), url
        assert "Błąd" in r.text and '"detail"' not in r.text
    r = c.get("/items/abc", headers=HX)
    assert r.status_code == 404 and r.headers["hx-reswap"] == "none"


def test_a_real_bug_gets_a_friendly_page_and_a_log_line(auth_client, caplog):
    app = auth_client.app

    @app.get("/_bug")
    def bug():
        raise RuntimeError("tajny szczegół implementacji")

    c = _client(auth_client)
    r = c.get("/_bug")
    assert r.status_code == 500 and "Coś poszło nie tak" in r.text and "Wróć na pulpit" in r.text
    assert "tajny szczegół" not in r.text and "Traceback" not in r.text
    r = c.get("/_bug", headers=HX)
    assert r.status_code == 500 and r.headers["hx-reswap"] == "none"
    assert any("unhandled error on GET /_bug" in rec.getMessage() for rec in caplog.records)
    assert any("tajny szczegół" in (rec.exc_text or "") or rec.exc_info for rec in caplog.records)


def test_htmx_fragments_for_every_id_route_on_seeded_data(auth_client, db, user):
    """Every GET route with an id, opened as a window (htmx) and as a page, for every seeded row."""
    _seed(db, user)
    c = _client(auth_client)
    bad = []
    for method, path, _ in _routes(c.app):
        if method != "GET" or "{" not in path:
            continue
        for i in (1, 2, 3):
            url = re.sub(r"\{[a-z_]+\}", str(i), path)
            for h in ({}, HX):
                if c.get(url, headers=h).status_code >= 500:
                    bad.append(url)
    assert bad == []
