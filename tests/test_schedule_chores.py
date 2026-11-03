"""Week schedule (/grafik) and house chores with rotation (/obowiazki): logic, push digests, pages, migration."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from domownik import chores, notify, schedule, webpush
from domownik.config import today
from domownik.models import (
    Chore,
    ChoreDone,
    Item,
    NotificationLog,
    PushSubscription,
    ScheduleEvent,
    ScheduleSkip,
    Task,
    User,
)
from domownik.security import hash_password

HX = {"HX-Request": "true"}
MON = date(2026, 11, 2)  # a Monday


@pytest.fixture
def ola(db) -> User:
    u = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


def _event(db, **kw) -> ScheduleEvent:
    ev = ScheduleEvent(
        title=kw.pop("title", "Robotyka"),
        weekdays=kw.pop("weekdays", "1"),
        starts_at=kw.pop("starts_at", "17:00"),
        ends_at=kw.pop("ends_at", "18:00"),
        day_assignees=kw.pop("day_assignees", {}),
        **kw,
    )
    db.add(ev)
    db.commit()
    return ev


def _chore(db, name="Łazienka", every=7, rotation=(), **kw) -> Chore:
    c = Chore(name=name, every_days=every, rotation=list(rotation), **kw)
    db.add(c)
    db.commit()
    return c


# ---------------------------------------------------------------- schedule logic


def test_occurrences_in_a_week_respect_period_and_called_off_day(db, user):
    ev = _event(db, weekdays="0,2,4", starts_on=MON + timedelta(days=1), ends_on=MON + timedelta(days=10))
    db.add(ScheduleSkip(event_id=ev.id, on_date=MON + timedelta(days=4)))
    db.commit()

    week = schedule.occurrences(db, MON, MON + timedelta(days=6))
    assert [(o.day.weekday(), o.cancelled) for o in week] == [(2, False), (4, True)]  # Monday before start
    assert schedule.occurrences(db, MON, MON + timedelta(days=6), cancelled=False)[0].day == MON + timedelta(days=2)
    nxt = schedule.occurrences(db, MON + timedelta(days=7), MON + timedelta(days=13))
    assert [o.day.weekday() for o in nxt] == [0, 2]  # ends on Thursday → no Friday
    assert all(not o.cancelled for o in nxt)  # calling off one day does not touch other weeks


def test_toggle_skip_and_wrong_day(db, user):
    ev = _event(db, weekdays="1")
    tue = MON + timedelta(days=1)
    assert schedule.toggle_skip(db, ev, tue, user) is True
    db.commit()
    assert schedule.occurrences(db, tue, tue)[0].cancelled
    assert schedule.toggle_skip(db, ev, tue, user) is False
    db.commit()
    assert db.scalars(select(ScheduleSkip)).all() == []
    with pytest.raises(ValueError):
        schedule.toggle_skip(db, ev, MON, user)


def test_person_per_weekday_and_moje(db, user, ola):
    _event(
        db,
        title="Odbiór z przedszkola",
        weekdays="0,1,2,3,4",
        starts_at="16:00",
        ends_at=None,
        assign_mode="days",
        day_assignees={"0": user.id, "1": ola.id, "2": user.id, "3": ola.id, "4": None},
    )
    week = schedule.occurrences(db, MON, MON + timedelta(days=6))
    assert [o.assignee.name if o.assignee else None for o in week] == ["Jan", "Ola", "Jan", "Ola", None]
    mine = schedule.occurrences(db, MON, MON + timedelta(days=6), user_id=ola.id)
    assert [o.day.weekday() for o in mine] == [1, 3, 4]  # her days + the shared Friday


def test_alternating_weeks(db, user, ola):
    ev = _event(db, weekdays="5", assign_mode="weeks", assignee_id=user.id, alt_assignee_id=ola.id, rota_anchor=MON)
    sat = MON + timedelta(days=5)
    who = [schedule.responsible_id(ev, sat + timedelta(weeks=k)) for k in range(-2, 3)]
    assert who == [user.id, ola.id, user.id, ola.id, user.id]
    assert schedule.on_duty_this_week(ev, MON + timedelta(days=7)) == (ola.id, user.id)


def test_labels_and_time_parsing():
    assert schedule.days_label([0, 1, 2, 3, 4]) == "pn–pt"
    assert schedule.days_label([1, 3]) == "wt, czw"
    assert schedule.days_label(list(range(7))) == "codziennie"
    assert schedule.parse_time("7:05") == "07:05" and schedule.parse_time("16.30") == "16:30"
    assert schedule.parse_time("") is None
    with pytest.raises(ValueError):
        schedule.parse_time("25:00")


# ---------------------------------------------------------------- chores logic


def test_rotation_moves_on_after_done_and_undo_gives_the_turn_back(db, user, ola):
    c = _chore(db, rotation=[user.id, ola.id])
    assert chores.turn_id(c) == user.id
    chores.complete(db, c, by=user, on=MON)
    db.commit()
    assert c.turn_user_id == ola.id
    chores.complete(db, c, by=ola, on=MON + timedelta(days=7))
    db.commit()
    assert c.turn_user_id == user.id and len(c.history) == 2

    assert chores.undo_last(db, c)
    db.commit()
    assert c.turn_user_id == ola.id and [h.done_by_id for h in c.history] == [user.id]
    assert chores.undo_last(db, c) and c.turn_user_id == user.id
    assert not chores.undo_last(db, c)


def test_done_for_someone_else_continues_from_the_actual_performer(db, user, ola):
    third = User(email="x@example.com", name="Ela", password_hash="x")
    db.add(third)
    db.commit()
    c = _chore(db, rotation=[user.id, ola.id, third.id])
    chores.complete(db, c, by=ola, on=MON)  # Jan's turn, Ola did it
    assert c.turn_user_id == third.id  # the one after Ola – Jan is not skipped twice later
    rec = c.history[0]
    assert rec.done_by_id == ola.id and rec.turn_user_id == user.id
    chores.undo_last(db, c)
    assert c.turn_user_id == user.id

    outsider = User(email="y@example.com", name="Gość", password_hash="x")
    db.add(outsider)
    db.commit()
    chores.complete(db, c, by=outsider, on=MON)  # not in the rotation → moves on from whose turn it was
    assert c.turn_user_id == ola.id


def test_rotation_edit_keeps_or_resets_the_turn(db, user, ola):
    c = _chore(db, rotation=[user.id, ola.id], turn_user_id=ola.id)
    chores.set_rotation(c, [ola.id, user.id, ola.id])
    assert c.rotation == [ola.id, user.id] and c.turn_user_id == ola.id
    chores.set_rotation(c, [user.id])
    assert c.turn_user_id == user.id
    chores.set_rotation(c, [])
    assert c.turn_user_id is None and chores.turn_id(c) is None


def test_freshness_levels_and_urgency_order(db, user):
    t = MON
    fresh = _chore(db, "Odkurzanie", 7)
    soon = _chore(db, "Kuchnia", 4)
    due = _chore(db, "Śmieci", 3)
    late = _chore(db, "Okna", 10)
    never = _chore(db, "Lodówka", 30)
    for c, ago in ((fresh, 1), (soon, 3), (due, 3), (late, 15)):
        db.add(ChoreDone(chore_id=c.id, done_on=t - timedelta(days=ago), done_by_id=user.id))
    db.commit()
    db.expire_all()

    sts = chores.states(db, t)
    assert [s.chore.name for s in sts] == ["Okna", "Śmieci", "Lodówka", "Kuchnia", "Odkurzanie"]
    by = {s.chore.name: s for s in sts}
    assert by["Okna"].level == "overdue" and by["Okna"].days_left == -5 and by["Okna"].fresh_pct == 0
    assert by["Śmieci"].level == "today" and by["Lodówka"].level == "new"
    assert by["Kuchnia"].level == "soon" and by["Odkurzanie"].level == "ok"
    assert by["Odkurzanie"].fresh_pct == 86 and by["Odkurzanie"].hue > by["Kuchnia"].hue
    assert [s.chore.name for s in sts if s.overdue] == ["Okna"]
    assert never.id  # never done is "do zrobienia", not overdue


def test_moje_for_chores(db, user, ola):
    _chore(db, "A", rotation=[user.id, ola.id])  # Jan's turn
    _chore(db, "B", rotation=[ola.id])
    _chore(db, "C")  # anyone
    assert {s.chore.name for s in chores.states(db, MON, user_id=user.id)} == {"A", "C"}
    assert {s.chore.name for s in chores.states(db, MON, user_id=ola.id)} == {"B", "C"}


# ---------------------------------------------------------------- push digests


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []

    def fake_deliver(sub, data, *, urgent=False):
        out.append(json.loads(data) | {"_endpoint": sub.endpoint})
        return True

    monkeypatch.setattr(webpush, "deliver", fake_deliver)
    return out


def _device(db, u, n):
    db.add(PushSubscription(user_id=u.id, endpoint=f"https://push.example.com/{n}", p256dh="pk", auth="au"))
    db.commit()


def _digest(db, slot, day, u) -> notify.Digest:
    return next(d for d in notify.build_digests(db, slot, day) if d.user.id == u.id)


def test_schedule_line_goes_to_the_person_on_duty_and_shared_to_everyone(db, user, ola):
    _device(db, user, 1)
    _device(db, ola, 2)
    t = today()
    kid = Item(name="Dziecko 1", kind="child")
    db.add(kid)
    db.commit()
    wd = str(t.weekday())
    _event(db, title="Odbiór", weekdays=wd, starts_at="16:00", assignee_id=ola.id, item_id=kid.id)
    _event(db, title="Sprzątanie", weekdays=wd, starts_at="09:00")  # shared
    _event(db, title="Basen", weekdays=wd, starts_at="18:00", assignee_id=user.id, notify=False)
    off = _event(db, title="Rytmika", weekdays=wd, starts_at="10:00")
    db.add(ScheduleSkip(event_id=off.id, on_date=t))
    db.commit()

    jan, o = _digest(db, "morning", t, user), _digest(db, "morning", t, ola)
    assert jan.message() == "🗓️ Grafik: 09:00 Sprzątanie"
    assert o.message() == "🗓️ Grafik: 09:00 Sprzątanie, 16:00 Odbiór (Dziecko 1)"
    assert jan.title() == "Domownik: grafik na dziś"
    # evening: tomorrow's slots
    _event(db, title="Robotyka", weekdays=str((t.weekday() + 1) % 7), starts_at="17:00", assignee_id=user.id)
    ev_jan = _digest(db, "evening", t, user)
    assert ev_jan.message() == "🗓️ Jutro w grafiku: 17:00 Robotyka"
    assert "Robotyka" not in _digest(db, "evening", t, ola).message()


def test_overdue_chores_one_line_only_for_whose_turn_and_never_cut_off(db, user, ola):
    _device(db, user, 1)
    t = today()
    for i in range(14):  # many task lines
        db.add(Item(id=100 + i, name=f"Auto {i}", kind="car"))
        db.add(Task(item_id=100 + i, title=f"Przegląd {i}", category="service", next_due_date=t - timedelta(days=1)))
    a = _chore(db, "Łazienka", 7, rotation=[user.id])
    b = _chore(db, "Śmieci", 3)
    c = _chore(db, "Okna", 7, rotation=[ola.id])
    d = _chore(db, "Pościel", 14, rotation=[user.id])
    for ch, ago in ((a, 9), (b, 4), (c, 30), (d, 2)):
        db.add(ChoreDone(chore_id=ch.id, done_on=t - timedelta(days=ago)))
    db.commit()

    dg = _digest(db, "morning", t, user)
    msg = dg.message()
    assert msg.count("🧹") == 1 and msg.endswith(
        "🧹 Obowiązki: śmieci, łazienka"
    )  # most urgent first; not Ola's, not fresh
    assert "…i 2 więcej" in msg
    assert "🧹" not in _digest(db, "evening", t, user).message()


def test_chores_alone_are_pushed_at_most_every_few_days_and_slots_not_twice(db, user, sent):
    _device(db, user, 1)
    t = today()
    c = _chore(db, "Łazienka", 7)
    db.add(ChoreDone(chore_id=c.id, done_on=t - timedelta(days=10)))
    db.commit()

    assert notify.run_slot(db, "morning", t) == 1
    assert sent[-1]["body"] == "🧹 Obowiązki: łazienka" and sent[-1]["title"] == "Domownik: obowiązki"
    assert notify.run_slot(db, "morning", t) == 0  # same day: the slot already ran
    assert db.scalar(select(NotificationLog).where(NotificationLog.kind == "chores")) is not None

    assert notify.run_slot(db, "morning", t + timedelta(days=1)) == 0  # chores only → not again tomorrow
    assert notify.run_slot(db, "morning", t + timedelta(days=notify.OVERDUE_REPEAT_DAYS)) == 1

    # with a schedule slot the chores line rides along every morning
    day = t + timedelta(days=notify.OVERDUE_REPEAT_DAYS + 1)
    _event(db, title="Basen", weekdays=str(day.weekday()), starts_at="17:00")
    assert notify.run_slot(db, "morning", day) == 1
    assert sent[-1]["body"] == "🗓️ Grafik: 17:00 Basen\n🧹 Obowiązki: łazienka"
    assert sent[-1]["actions"] == []
    assert len(sent) == 3


# ---------------------------------------------------------------- pages


def _form(**kw):
    data = {"title": "Basen", "days": ["1", "3"], "starts_at": "17:00", "ends_at": "18:00", "who_mode": "shared"}
    data.update(kw)
    return data


def test_schedule_create_validate_edit_cancel_delete(auth_client, db, user, ola):
    r = auth_client.post("/grafik", data=_form(days=[]), headers=HX)
    assert r.status_code == 422 and r.headers["HX-Retarget"] == "#sched-error" and "dzień" in r.text
    r = auth_client.post("/grafik", data=_form(ends_at="16:00"), headers=HX)
    assert r.status_code == 422 and "po początku" in r.text
    r = auth_client.post("/grafik", data=_form(who_mode="weeks", week_a=user.id, week_b=user.id), headers=HX)
    assert r.status_code == 422 and "dwie różne" in r.text
    assert db.scalars(select(ScheduleEvent)).all() == []

    kid = Item(name="Dziecko 1", kind="child")
    db.add(kid)
    db.commit()
    r = auth_client.post(
        "/grafik",
        data=_form(
            title="Odbiór z przedszkola",
            days=["0", "1", "2", "3", "4"],
            starts_at="16:00",
            ends_at="",
            item_id=kid.id,
            who_mode="days",
            day_0=user.id,
            day_1=ola.id,
            day_2="",
            day_5=ola.id,  # not a ticked day – ignored
            notify="1",
        ),
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["HX-Redirect"] == "/grafik"
    ev = db.scalars(select(ScheduleEvent)).one()
    assert ev.days == [0, 1, 2, 3, 4] and ev.ends_at is None and ev.item_id == kid.id and ev.notify
    assert ev.day_assignees == {"0": user.id, "1": ola.id, "2": None, "3": None, "4": None}

    page = auth_client.get(f"/grafik?w={MON.isoformat()}").text
    assert page.count("Odbiór z przedszkola") >= 5 and "🧒 Dziecko 1" in page and "pn–pt" in page
    assert auth_client.get(f"/grafik?w={MON.isoformat()}&who=me").text.count('class="wk-occ') == 4  # not Ola's Tue

    tue = MON + timedelta(days=1)
    r = auth_client.post(f"/grafik/{ev.id}/odwolaj", data={"d": tue.isoformat(), "who": ""}, follow_redirects=False)
    current = MON == schedule.week_start(today())
    assert r.headers["HX-Redirect"] == ("/grafik" if current else f"/grafik?w={MON.isoformat()}")
    db.expire_all()
    assert [s.on_date for s in db.get(ScheduleEvent, ev.id).skips] == [tue]
    assert "przywróć" in auth_client.get(f"/grafik?w={MON.isoformat()}").text
    assert (
        auth_client.post(f"/grafik/{ev.id}/odwolaj", data={"d": (MON + timedelta(days=5)).isoformat()}).status_code
        == 400
    )

    assert "Zmień wpis grafiku" in auth_client.get(f"/grafik/{ev.id}").text
    auth_client.post(f"/grafik/{ev.id}", data=_form(who_mode="weeks", week_a=ola.id, week_b=user.id))
    db.expire_all()
    ev = db.get(ScheduleEvent, ev.id)
    assert ev.assign_mode == "weeks" and schedule.responsible_id(ev, today()) == ola.id
    assert ev.day_assignees == {} and ev.item_id is None and not ev.notify

    auth_client.post(f"/grafik/{ev.id}/usun")
    db.expire_all()
    assert db.scalars(select(ScheduleEvent)).all() == [] and db.scalars(select(ScheduleSkip)).all() == []
    assert "Stały plan tygodnia" in auth_client.get("/grafik").text


def test_chores_pages_add_suggest_done_undo_edit_delete(auth_client, db, user, ola):
    page = auth_client.get("/obowiazki").text
    assert "Brak obowiązków" in page and "＋ Odkurzanie" in page

    r = auth_client.post("/obowiazki", data={"name": "", "every_days": "7"}, headers=HX)
    assert r.status_code == 422 and r.headers["HX-Retarget"] == "#chore-error-new"
    r = auth_client.post("/obowiazki", data={"name": "Kwiaty", "every_days": "0"}, headers=HX)
    assert r.status_code == 422

    r = auth_client.post(
        "/obowiazki",
        data={
            "name": "Podlewanie kwiatów",
            "every_days": "4",
            "room": "Salon",
            "est_minutes": "10",
            "rotation_sent": "1",
            "rotation": [str(ola.id), str(user.id)],
        },
        follow_redirects=False,
    )
    c = db.scalars(select(Chore)).one()
    assert r.headers["HX-Redirect"] == f"/obowiazki#ch{c.id}"
    assert c.rotation == [user.id, ola.id] and c.room == "Salon" and c.est_minutes == 10

    auth_client.post("/obowiazki/propozycja", data={"name": "Łazienka"})
    auth_client.post("/obowiazki/propozycja", data={"name": "Łazienka"})  # no duplicate
    bath = db.scalars(select(Chore).where(Chore.name == "Łazienka")).one()
    assert bath.every_days == 7 and bath.rotation == [user.id, ola.id]
    assert "＋ Łazienka" not in auth_client.get("/obowiazki").text

    auth_client.post(f"/obowiazki/{c.id}/zrobione")
    db.expire_all()
    c = db.get(Chore, c.id)
    assert c.history[0].done_by_id == user.id and c.turn_user_id == ola.id
    html = auth_client.get("/obowiazki").text
    assert "Teraz:" in html and "Ostatnio: Jan" in html

    auth_client.post(f"/obowiazki/{c.id}/zrobione", data={"by": str(ola.id)})  # ticked off for Ola
    db.expire_all()
    c = db.get(Chore, c.id)
    assert c.history[0].done_by_id == ola.id and c.turn_user_id == user.id
    auth_client.post(f"/obowiazki/{c.id}/cofnij")
    db.expire_all()
    c = db.get(Chore, c.id)
    assert len(c.history) == 1 and c.turn_user_id == ola.id

    assert {s.chore.name for s in chores.states(db, today(), user_id=user.id)} == {"Łazienka"}
    assert f'id="ch{c.id}"' not in auth_client.get("/obowiazki?who=me").text

    auth_client.post(
        f"/obowiazki/{c.id}",
        data={"name": "Kwiaty", "every_days": "5", "room": "", "rotation_sent": "1", "rotation": [str(user.id)]},
    )
    db.expire_all()
    c = db.get(Chore, c.id)
    assert (c.name, c.every_days, c.room, c.rotation, c.turn_user_id) == ("Kwiaty", 5, None, [user.id], user.id)
    cid = c.id
    auth_client.post(f"/obowiazki/{cid}/usun")
    db.expire_all()
    assert db.get(Chore, cid) is None and db.scalars(select(ChoreDone).where(ChoreDone.chore_id == cid)).all() == []


def test_dashboard_today_card_nav_and_calendar_day(auth_client, db, user):
    db.add(Item(name="Dom", kind="house"))
    db.commit()
    html = auth_client.get("/").text
    assert 'class="today-card"' not in html  # nothing today → no card
    assert 'href="/grafik"' in html and 'href="/obowiazki"' in html  # sidebar / "Więcej"
    assert html.count('class="bottombar"') == 1 and "/grafik" not in html.split('class="bottombar"')[1]

    t = today()
    _event(db, title="Rytmika", weekdays=str(t.weekday()), starts_at="10:00")
    c = _chore(db, "Okna", 7)
    db.add(ChoreDone(chore_id=c.id, done_on=t - timedelta(days=8)))
    db.commit()
    html = auth_client.get("/").text
    assert html.index('class="timeline-card"') < html.index('class="today-card"')  # under the timeline
    assert "Rytmika" in html and "1 zaległe" in html and "okna" in html
    assert "Rytmika" not in auth_client.get(f"/api/timeline?start={t}&end={t}").text  # never on the timeline

    cal = auth_client.get(f"/kalendarz/dzien?d={t.isoformat()}").text
    assert "Grafik" in cal and "Rytmika" in cal
    other_day = auth_client.get(f"/kalendarz?d={(t + timedelta(days=1)).isoformat()}").text
    assert "Rytmika" not in other_day  # month grid stays clean; only the day list shows the schedule


# ---------------------------------------------------------------- migration


def test_migration_0011_up_and_down(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")  # like `domownik migrate`
        conn.commit()
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "51c48ad82469")
            conn.execute(
                sa.text("INSERT INTO user (id, name, email, password_hash, is_active) VALUES (1, 'A', 'a@x', 'h', 1)")
            )
            conn.execute(
                sa.text(
                    "INSERT INTO item (id, kind, name, sort_order, attributes, archived) VALUES (1, 'child', 'D', 0, '{}', 0)"
                )
            )
            command.upgrade(cfg, "head")
            conn.execute(
                sa.text(
                    "INSERT INTO schedule_event (id, title, weekdays, starts_at, item_id, assignee_id, day_assignees) "
                    "VALUES (1, 'Basen', '2', '17:00', 1, 1, '{}')"
                )
            )
            conn.execute(sa.text("INSERT INTO schedule_skip (event_id, on_date) VALUES (1, '2026-11-04')"))
            conn.execute(
                sa.text("INSERT INTO chore (id, name, every_days, rotation, turn_user_id) VALUES (1, 'X', 7, '[1]', 1)")
            )
            conn.execute(sa.text("INSERT INTO chore_done (chore_id, done_on, done_by_id) VALUES (1, '2026-11-01', 1)"))
            row = conn.execute(sa.text("SELECT assign_mode, notify FROM schedule_event")).one()
            assert tuple(row) == ("fixed", 1)  # server defaults
            command.downgrade(cfg, "51c48ad82469")
            tables = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
            assert not tables & {"schedule_event", "schedule_skip", "chore", "chore_done"}
            assert conn.execute(sa.text("SELECT count(*) FROM item")).scalar() == 1
            assert conn.execute(sa.text("SELECT count(*) FROM user")).scalar() == 1
            command.upgrade(cfg, "head")
