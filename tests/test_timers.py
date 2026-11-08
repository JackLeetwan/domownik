from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from domownik import timers, webpush
from domownik.models import PushSubscription, Timer, User
from domownik.security import hash_password

QUICK = {"HX-Request": "true", "X-Quick-Add": "1"}
T0 = datetime(2026, 11, 8, 14, 0)


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []

    def fake_deliver(sub, data, *, urgent=False):
        out.append(json.loads(data) | {"_endpoint": sub.endpoint})
        return True

    monkeypatch.setattr(webpush, "deliver", fake_deliver)
    return out


@pytest.fixture
def paula(db):
    u = User(email="paula@example.com", name="Paula", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


def _device(db, user, endpoint):
    db.add(PushSubscription(user_id=user.id, endpoint=endpoint, p256dh="pk", auth="au"))
    db.commit()


def _to(sent):
    return sorted(s["_endpoint"] for s in sent)


# ---------------------------------------------------------------- pushes


def test_push_to_starter_then_household_once(db, user, paula, sent):
    _device(db, user, "https://push.example.com/jan")
    _device(db, paula, "https://push.example.com/paula")
    timers.start(db, user, "Pranie", 120, at=T0)

    assert timers.run(db, T0 + timedelta(minutes=119)) == 0
    assert timers.run(db, T0 + timedelta(minutes=120)) == 1
    assert _to(sent) == ["https://push.example.com/jan"]
    assert sent[0]["title"] == "⏱ Pranie – koniec"
    assert sent[0]["body"] == "Minęło 2 godz. (start 14:00)."
    assert [a["title"] for a in sent[0]["actions"]] == ["✓ Zrobione", "+15 min"]

    assert timers.run(db, T0 + timedelta(minutes=134)) == 0  # nobody reacted, but not 15 min yet
    sent.clear()
    assert timers.run(db, T0 + timedelta(minutes=135)) == 2
    assert _to(sent) == ["https://push.example.com/jan", "https://push.example.com/paula"]
    assert sent[0]["body"].startswith("Czeka już 15 min")
    assert timers.run(db, T0 + timedelta(minutes=200)) == 0  # no third push


def test_two_laundries_the_same_day_both_push(db, user, sent):
    _device(db, user, "https://push.example.com/jan")
    first = timers.start(db, user, "Pranie", 60, at=T0)
    timers.run(db, T0 + timedelta(minutes=60))
    timers.finish(db, first, T0 + timedelta(minutes=61))
    timers.start(db, user, "Pranie", 60, at=T0 + timedelta(minutes=62))
    assert timers.run(db, T0 + timedelta(minutes=122)) == 1
    assert len(sent) == 2


def test_starter_without_device_means_everyone(db, user, paula, sent):
    _device(db, paula, "https://push.example.com/paula")
    timers.start(db, user, "Pranie", 60, at=T0)
    assert timers.run(db, T0 + timedelta(minutes=61)) == 1
    assert _to(sent) == ["https://push.example.com/paula"]


def test_done_stops_and_extend_starts_over(db, user, paula, sent):
    _device(db, user, "https://push.example.com/jan")
    _device(db, paula, "https://push.example.com/paula")
    t = timers.start(db, user, "Pranie", 60, at=T0)
    timers.run(db, T0 + timedelta(minutes=60))
    timers.extend(db, t, T0 + timedelta(minutes=65))
    assert t.ends_at == T0 + timedelta(minutes=80)
    assert timers.run(db, T0 + timedelta(minutes=79)) == 0
    assert timers.run(db, T0 + timedelta(minutes=80)) == 1  # the starter again, not the household
    timers.finish(db, t, T0 + timedelta(minutes=81))
    assert timers.run(db, T0 + timedelta(minutes=120)) == 0
    assert _to(sent) == ["https://push.example.com/jan", "https://push.example.com/jan"]


def test_extend_before_the_end_adds_to_the_end(db, user):
    t = timers.start(db, user, "Piekarnik", 30, at=T0)
    timers.extend(db, t, T0 + timedelta(minutes=10))
    assert t.ends_at == T0 + timedelta(minutes=45)


def test_server_back_late_is_quiet_and_old_timers_close(db, user, sent):
    _device(db, user, "https://push.example.com/jan")
    t = timers.start(db, user, "Pranie", 60, at=T0)
    assert timers.run(db, T0 + timedelta(hours=5)) == 0  # ended 4 h ago – no push about old laundry
    assert t.notified_at is not None and t.finished_at is None
    timers.run(db, T0 + timedelta(hours=13, minutes=1))
    assert t.finished_at is not None
    assert timers.active(db) == []


def test_a_failing_push_service_is_not_retried_every_minute(db, user, monkeypatch):
    _device(db, user, "https://push.example.com/jan")
    calls = []

    def broken(sub, data, *, urgent=False):
        calls.append(1)
        raise webpush.PushError("down")

    monkeypatch.setattr(webpush, "deliver", broken)
    timers.start(db, user, "Pranie", 60, at=T0)
    timers.run(db, T0 + timedelta(minutes=60))
    timers.run(db, T0 + timedelta(minutes=61))
    assert len(calls) == 1


# ---------------------------------------------------------------- buttons in the push


def test_push_buttons(client, db, user):
    t = timers.start(db, user, "Pranie", 60, at=T0)
    tok = timers.token(t)
    r = client.post(f"/n/timer/{tok}/plus")
    assert r.status_code == 200 and r.text.startswith("Pranie: przypomnę o")
    r = client.post(f"/n/timer/{tok}/done")
    assert r.text == "Zrobione: Pranie"
    assert client.post(f"/n/timer/{tok}/done").text == "Już zakończone: Pranie"
    assert client.post("/n/timer/garbage/done").status_code == 403
    assert client.post(f"/n/timer/{tok}/explode").status_code == 403
    assert client.get(f"/n/timer/{tok}/done", follow_redirects=False).status_code == 303


# ---------------------------------------------------------------- the app


def test_quick_add_starts_a_timer(auth_client, db):
    page = auth_client.get("/dodaj/timer", headers={"HX-Request": "true"})
    assert page.status_code == 200
    assert 'value="Pranie"' in page.text and "3 godz." in page.text
    assert "⏱" in auth_client.get("/dodaj", headers={"HX-Request": "true"}).text

    r = auth_client.post("/timery", data={"title": "Pranie", "minutes": "120"}, headers=QUICK)
    assert r.status_code == 200 and "dm-added" in r.headers["HX-Trigger"]
    t = db.query(Timer).one()
    assert t.minutes == 120
    left = t.ends_at - timers._now()
    assert timedelta(minutes=118) < left <= timedelta(minutes=120)


def test_own_minutes_win_and_bad_ones_are_a_message(auth_client, db):
    auth_client.post("/timery", data={"title": "Zmywarka", "minutes": "60", "own_minutes": "45"}, headers=QUICK)
    assert db.query(Timer).one().minutes == 45
    for bad in ("0", "abc", str(timers.MAX_MINUTES + 1)):
        r = auth_client.post("/timery", data={"title": "x", "own_minutes": bad}, headers=QUICK)
        assert r.status_code == 422 and r.headers["HX-Retarget"] == "#qa-error"
    assert db.query(Timer).count() == 1


def test_recent_titles_are_suggested(auth_client, db, user):
    timers.start(db, user, "Pranie", 60, at=T0)
    timers.start(db, user, "Zmywarka", 60, at=T0)
    assert timers.recent_titles(db) == ["Zmywarka", "Pranie"]
    assert 'value="Zmywarka"' in auth_client.get("/dodaj/timer", headers={"HX-Request": "true"}).text


def test_dashboard_card_and_buttons(auth_client, db, user):
    from domownik.models import Item

    db.add(Item(name="Dom", kind="house"))
    db.commit()
    assert 'id="timers"' in auth_client.get("/").text  # always there, hidden when empty
    t = timers.start(db, user, "Pranie", 60)
    page = auth_client.get("/").text
    assert "⏱ Pranie" in page and "data-timer-ends" in page

    r = auth_client.post(f"/timery/{t.id}/plus", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "⏱ Pranie" in r.text
    db.refresh(t)
    assert t.minutes == 60 and t.notified_at is None
    r = auth_client.post(f"/timery/{t.id}/koniec", headers={"HX-Request": "true"})
    assert "⏱ Pranie" not in r.text and "hidden" in r.text
    assert auth_client.post("/timery/999/koniec", headers={"HX-Request": "true"}).status_code == 404


def test_migration_0031_up_and_down(tmp_path):
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
            assert conn.execute(sa.text("SELECT count(*) FROM timer")).scalar() == 0
            command.downgrade(cfg, "5b8e2d7c4a19")
            tables = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
            assert "timer" not in tables
            command.upgrade(cfg, "head")
