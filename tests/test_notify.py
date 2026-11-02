"""Push notifications: who gets what, deduplication, the one-tap 'done', settings page."""

from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import select

from domownik import notify
from domownik.config import get_settings, today
from domownik.models import Completion, Item, NotificationLog, Task, User
from domownik.security import hash_password


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(notify, "send", lambda body: out.append(body))
    return out


def _item(db) -> Item:
    item = Item(name="Dom", kind="house")
    db.add(item)
    db.commit()
    return item


def _task(db, item, title, due, **kw) -> Task:
    t = Task(
        item_id=item.id,
        title=title,
        category=kw.pop("category", "service"),
        recurrence=kw.pop("recurrence", {"mode": "interval", "every": 1, "unit": "year", "anchor": "due"}),
        next_due_date=due,
        lead_days=kw.pop("lead_days", 7),
        direction=kw.pop("direction", "expense"),
        amount_gr=kw.pop("amount_gr", 12345),
        **kw,
    )
    db.add(t)
    db.commit()
    return t


def _with_topic(db, user, topic="domownik-test-topic-1"):
    user.ntfy_topic = topic
    db.commit()
    return user


def test_morning_digest_picks_overdue_today_and_newly_soon(db, user):
    _with_topic(db, user)
    item = _item(db)
    t = today()
    _task(db, item, "Przegląd", t - timedelta(days=2))
    _task(db, item, "Śmieci", t)
    _task(db, item, "Serwis pompy", t + timedelta(days=5))
    _task(db, item, "Ubezpieczenie", t + timedelta(days=40))  # outside its 7-day window
    _task(db, item, "Netflix", t, autopay=True, category="payment")
    for name in ("Prąd", "Woda", "Gaz"):
        _task(db, item, name, t + timedelta(days=6), category="payment", recurrence={"mode": "monthly", "day": 1})

    (d,) = notify.build_digests(db, "morning", t)

    msg = d.message()
    assert "Przegląd – zaległe" in msg and "Dziś: Śmieci" in msg and "Serwis pompy" in msg
    assert "Opłaty – 3 pozycji" in msg  # same-day payments → one line
    assert "Ubezpieczenie" not in msg and "Netflix" not in msg
    assert d.title() == "Domownik: 1 zaległe · 1 na dziś · 4 wkrótce"
    assert "zł" not in msg and "123" not in msg  # no amounts through a public server


def test_assignee_gets_own_and_unassigned_only(db, user):
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo1"))
    db.add(other)
    db.commit()
    _with_topic(db, user)
    _with_topic(db, other, "domownik-test-topic-2")
    item = _item(db)
    t = today()
    _task(db, item, "Moje", t, assignee_id=user.id)
    _task(db, item, "Oli", t, assignee_id=other.id)
    _task(db, item, "Wspólne", t)

    by_name = {d.user.name: d.message() for d in notify.build_digests(db, "morning", t)}

    assert "Moje" in by_name["Jan"] and "Oli" not in by_name["Jan"] and "Wspólne" in by_name["Jan"]
    assert "Oli" in by_name["Ola"] and "Moje" not in by_name["Ola"] and "Wspólne" in by_name["Ola"]


def test_run_slot_once_a_day_and_dedup_over_days(db, user, sent):
    _with_topic(db, user)
    item = _item(db)
    t = today()
    _task(db, item, "Zaległe", t - timedelta(days=1))
    _task(db, item, "Wkrótce", t + timedelta(days=5))

    assert notify.run_slot(db, "morning", t) == 1
    assert notify.run_slot(db, "morning", t) == 0  # slot already done today
    body = sent[0]
    assert body["topic"] == user.ntfy_topic and body["priority"] == 4
    assert body["actions"][0]["label"] == "Otwórz"
    assert any(a["action"] == "http" and "/n/done/" in a["url"] for a in body["actions"])

    # next day: "soon" is not repeated, overdue waits for its 3-day repeat
    assert notify.run_slot(db, "morning", t + timedelta(days=1)) == 0
    assert notify.run_slot(db, "morning", t + timedelta(days=3)) == 1
    assert "Zaległe" in sent[-1]["message"] and "Wkrótce" not in sent[-1]["message"]


def test_today_announcement_does_not_hide_first_overdue_day(db, user, sent):
    _with_topic(db, user)
    t = today()
    _task(db, _item(db), "Rata", t)
    notify.run_slot(db, "morning", t)
    notify.run_slot(db, "morning", t + timedelta(days=1))
    assert "Rata – zaległe" in sent[-1]["message"]


def test_failed_send_is_retried(db, user, monkeypatch):
    import httpx

    _with_topic(db, user)
    _task(db, _item(db), "Śmieci", today())

    def boom(body):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(notify, "send", boom)
    assert notify.run_slot(db, "morning", today()) == 0
    assert db.scalar(select(NotificationLog)) is None  # nothing recorded → next tick tries again


def test_evening_is_about_tomorrow(db, user, sent):
    _with_topic(db, user)
    item = _item(db)
    t = today()
    _task(db, item, "Strój na bal", t + timedelta(days=1), category="event", lead_days=1)
    _task(db, item, "Dziś", t)
    notify.run_slot(db, "evening", t)
    assert sent[0]["title"] == "Domownik: na jutro"
    assert "Jutro: Strój na bal" in sent[0]["message"] and "Dziś" not in sent[0]["message"]


def test_switched_off_slot_and_no_topic(db, user, sent):
    _task(db, _item(db), "Śmieci", today())
    assert notify.run_slot(db, "morning", today()) == 0  # no topic
    _with_topic(db, user)
    user.notify_morning = False
    db.commit()
    assert notify.run_slot(db, "morning", today()) == 0


def test_tick_respects_hours(db, user, sent):
    s = get_settings()
    _with_topic(db, user)
    _task(db, _item(db), "Śmieci", today())
    notify.tick(db, datetime.combine(today(), time(s.morning_hour - 1, 50)))
    assert sent == []
    notify.tick(db, datetime.combine(today(), time(s.morning_hour + 1, 30)))  # catch-up after a late start
    assert len(sent) == 1


def test_done_from_push(client, db, user):
    item = _item(db)
    task = _task(db, item, "Śmieci", today())
    token = notify.done_token(task, user)

    r = client.post(f"/n/done/{token}")
    assert r.status_code == 200 and "Zrobione" in r.text
    db.refresh(task)
    assert task.next_due_date > today()
    comp = db.scalar(select(Completion).where(Completion.task_id == task.id))
    assert comp.performed_by_id == user.id and comp.actual_amount_gr == 12345

    r = client.post(f"/n/done/{token}")  # tapped twice
    assert "Już odhaczone" in r.text
    assert len(list(db.scalars(select(Completion)))) == 1
    assert client.post("/n/done/zly-token").status_code == 403


def test_settings_page(auth_client, db, user, sent):
    page = auth_client.get("/ustawienia")
    assert "Wygeneruj temat" in page.text

    auth_client.post("/ustawienia/temat")
    db.refresh(user)
    assert user.ntfy_topic and user.ntfy_topic.startswith("domownik-") and len(user.ntfy_topic) >= 20

    auth_client.post("/ustawienia", data={"ntfy_topic": user.ntfy_topic, "notify_morning": "1"})
    db.refresh(user)
    assert user.notify_morning is True and user.notify_evening is False

    r = auth_client.post("/ustawienia", data={"ntfy_topic": "krótki"})
    assert r.status_code == 422

    r = auth_client.post("/ustawienia/test")
    assert "Wysłano powiadomienie próbne" in r.text
    assert sent[-1]["topic"] == user.ntfy_topic
