from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from domownik import timers, webpush
from domownik.config import get_settings
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
    assert r.text == "Zrobione: Pranie · Suszarka? Start na pulpicie"
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
    t = timers.start(db, user, "Zmywarka", 60)
    page = auth_client.get("/").text
    assert "⏱ Zmywarka" in page and "data-timer-ends" in page

    r = auth_client.post(f"/timery/{t.id}/plus", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "⏱ Zmywarka" in r.text
    db.refresh(t)
    assert t.minutes == 60 and t.notified_at is None
    r = auth_client.post(f"/timery/{t.id}/koniec", headers={"HX-Request": "true"})
    assert "⏱ Zmywarka" not in r.text and "hidden" in r.text
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


# ---------------------------------------------------------------- stage 3: chores and the next step


def _chore(db, name="Pranie", **kw):
    from domownik.models import Chore

    c = Chore(name=name, every_days=3, rotation=[], uses_timer=True, **kw)
    db.add(c)
    db.commit()
    return c


def test_done_ticks_the_chore_off_with_points(db, user):
    from domownik.models import PointEntry

    c = _chore(db, points=5)
    t = timers.start(db, user, "", 120, at=T0, chore=c)
    assert t.title == "Pranie" and t.chore_id == c.id
    timers.finish(db, t, T0 + timedelta(minutes=125))
    db.refresh(c)
    assert len(c.history) == 1 and c.history[0].done_by_id == user.id
    assert db.query(PointEntry).one().points == 5
    timers.finish(db, t, T0 + timedelta(minutes=130))  # twice: nothing more
    db.refresh(c)
    assert len(c.history) == 1


def test_cancel_does_not_tick_and_offers_nothing(db, user):
    c = _chore(db)
    t = timers.start(db, user, "Pranie", 60, at=T0, chore=c)
    timers.cancel(db, t, T0 + timedelta(minutes=10))
    db.refresh(c)
    assert c.history == [] and t.finished_at is not None
    assert timers.pending_follow_up(db, T0 + timedelta(minutes=11)) is None


def test_quick_add_title_finds_the_chore(db, user):
    c = _chore(db)
    assert timers.start(db, user, "pranie", 60, at=T0).chore_id == c.id
    assert timers.start(db, user, "Zmywarka", 60, at=T0).chore_id is None


def test_push_done_ticks_the_chore_as_the_starter(client, db, user):
    c = _chore(db)
    t = timers.start(db, user, "Pranie", 60, at=T0, chore=c)
    r = client.post(f"/n/timer/{timers.token(t)}/done")
    assert r.text == "Zrobione: Pranie · Suszarka? Start na pulpicie"
    db.refresh(c)
    assert c.history[0].done_by_id == user.id


def test_follow_up_default_then_learned(db, user):
    t = timers.start(db, user, "Pranie", 60, at=T0)
    timers.finish(db, t, T0 + timedelta(minutes=60))
    f = timers.pending_follow_up(db, T0 + timedelta(minutes=70))
    assert (f.title, f.minutes, f.after.id) == ("Suszarka", 120, t.id)
    assert timers.pending_follow_up(db, T0 + timedelta(minutes=91)) is None  # the window has passed

    # this household hangs it out instead – started right after "Pranie" ended
    nxt = timers.start(db, user, "Rozwieszenie", 20, at=T0 + timedelta(minutes=65))
    local_start = T0 + timedelta(minutes=65)
    nxt.created_at = local_start.replace(tzinfo=get_settings().tz).astimezone(UTC).replace(tzinfo=None)
    db.commit()
    assert timers.pending_follow_up(db, T0 + timedelta(minutes=66)) is None  # something was started already
    timers.finish(db, nxt, T0 + timedelta(minutes=85))

    again = timers.start(db, user, "Pranie", 60, at=T0 + timedelta(hours=5))
    timers.finish(db, again, T0 + timedelta(hours=6))
    assert timers.next_step(db, again) == ("Rozwieszenie", 20)


def test_follow_up_on_dashboard_card(auth_client, db, user):
    t = timers.start(db, user, "Pranie", 60)
    r = auth_client.post(f"/timery/{t.id}/koniec", headers={"HX-Request": "true"})
    assert "Teraz Suszarka?" in r.text and "Start 2 godz." in r.text
    r = auth_client.post("/timery", data={"title": "Suszarka", "minutes": "120"}, headers={"HX-Request": "true"})
    assert "⏱ Suszarka" in r.text and "Teraz Suszarka?" not in r.text

    t2 = db.query(Timer).filter_by(title="Suszarka").one()
    r = auth_client.post(f"/timery/{t2.id}/koniec", headers={"HX-Request": "true"})
    assert "Teraz" not in r.text  # no default after the dryer
    t3 = timers.start(db, user, "Pranie", 60)
    timers.finish(db, t3)
    r = auth_client.post(f"/timery/{t3.id}/bez-dalej", headers={"HX-Request": "true"})
    assert "Teraz" not in r.text


def test_chore_row_buttons_start_a_timer(auth_client, db, user):
    c = _chore(db)
    page = auth_client.get("/obowiazki").text
    assert "⏱ Start:" in page and "3 godz." in page
    r = auth_client.post(f"/obowiazki/{c.id}/timer", data={"minutes": "180"}, follow_redirects=False)
    assert r.status_code == 303
    t = db.query(Timer).one()
    assert (t.title, t.minutes, t.chore_id) == ("Pranie", 180, c.id)
    assert "⏱ Trwa – koniec o" in auth_client.get("/obowiazki").text
    timers.finish(db, t)
    page = auth_client.get("/obowiazki").text
    assert 'title="Ostatnio używany czas">3 godz.' in page


def test_chore_suggestion_and_switch(auth_client, db):
    from domownik.models import Chore

    auth_client.post("/obowiazki/propozycja", data={"name": "Pranie"})
    c = db.query(Chore).filter_by(name="Pranie").one()
    assert c.uses_timer
    auth_client.post(f"/obowiazki/{c.id}", data={"name": "Pranie", "every_days": "3", "timer_sent": "1"})
    db.refresh(c)
    assert not c.uses_timer
    auth_client.post(f"/obowiazki/{c.id}", data={"name": "Pranie", "every_days": "3"})  # old form: unchanged
    db.refresh(c)
    assert not c.uses_timer


def test_migration_0032_marks_laundry(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn:
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "7c3f1a9e2b60")
            conn.execute(sa.text("INSERT INTO chore (name, every_days, rotation) VALUES ('Pranie', 3, '[]')"))
            conn.execute(sa.text("INSERT INTO chore (name, every_days, rotation) VALUES ('Łazienka', 7, '[]')"))
            command.upgrade(cfg, "head")
            got = dict(conn.execute(sa.text("SELECT name, uses_timer FROM chore")).all())
            assert got == {"Pranie": 1, "Łazienka": 0}
            command.downgrade(cfg, "7c3f1a9e2b60")
            command.upgrade(cfg, "head")
