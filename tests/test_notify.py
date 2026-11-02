"""Push notifications: who gets what, deduplication, the one-tap 'done', devices, settings page."""

import base64
import json
import os
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import select

from domownik import notify, webpush
from domownik.config import get_settings, today
from domownik.models import Completion, Item, NotificationLog, PushSubscription, Task, User
from domownik.security import hash_password


@pytest.fixture
def sent(monkeypatch):
    """Every delivered push as (subscription endpoint, decoded payload)."""
    out: list[dict] = []

    def fake_deliver(sub, data, *, urgent=False):
        out.append(json.loads(data) | {"_endpoint": sub.endpoint, "_urgent": urgent})
        return True

    monkeypatch.setattr(webpush, "deliver", fake_deliver)
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


def _with_topic(db, user, endpoint="https://push.example.com/dev-1"):
    """A user with one phone that enabled notifications."""
    db.add(PushSubscription(user_id=user.id, endpoint=endpoint, p256dh="pk", auth="au", label="Android · Chrome"))
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
    assert "−123,45\xa0zł" in msg and "Opłaty – 3 pozycji · −370,35\xa0zł" in msg  # encrypted end-to-end


def test_assignee_gets_own_and_unassigned_only(db, user):
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo1"))
    db.add(other)
    db.commit()
    _with_topic(db, user)
    _with_topic(db, other, "https://push.example.com/dev-2")
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
    assert body["_endpoint"] == "https://push.example.com/dev-1" and body["_urgent"] is True
    assert body["url"] == "/" and body["tag"] == "domownik-morning"
    assert body["actions"][0]["title"].startswith("✓ ") and body["actions"][0]["url"].startswith("/n/done/")

    # next day: "soon" is not repeated, overdue waits for its 3-day repeat
    assert notify.run_slot(db, "morning", t + timedelta(days=1)) == 0
    assert notify.run_slot(db, "morning", t + timedelta(days=3)) == 1
    assert "Zaległe" in sent[-1]["body"] and "Wkrótce" not in sent[-1]["body"]


def test_today_announcement_does_not_hide_first_overdue_day(db, user, sent):
    _with_topic(db, user)
    t = today()
    _task(db, _item(db), "Rata", t)
    notify.run_slot(db, "morning", t)
    notify.run_slot(db, "morning", t + timedelta(days=1))
    assert "Rata – zaległe" in sent[-1]["body"]


def test_failed_send_is_retried(db, user, monkeypatch):
    _with_topic(db, user)
    _task(db, _item(db), "Śmieci", today())

    def boom(sub, data, *, urgent=False):
        raise webpush.PushError("offline")

    monkeypatch.setattr(webpush, "deliver", boom)
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
    assert "Jutro: Strój na bal" in sent[0]["body"] and "Dziś" not in sent[0]["body"]


def test_switched_off_slot_and_no_topic(db, user, sent):
    _task(db, _item(db), "Śmieci", today())
    assert notify.run_slot(db, "morning", today()) == 0  # no device
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


def test_expired_device_is_removed(db, user, monkeypatch):
    _with_topic(db, user)
    _with_topic(db, user, "https://push.example.com/dev-old")
    _task(db, _item(db), "Śmieci", today())
    got = []

    def deliver(sub, data, *, urgent=False):
        got.append(sub.endpoint)
        return not sub.endpoint.endswith("old")  # 410 Gone for the old phone

    monkeypatch.setattr(webpush, "deliver", deliver)
    assert notify.run_slot(db, "morning", today()) == 1
    assert len(got) == 2
    assert [s.endpoint for s in db.scalars(select(PushSubscription))] == ["https://push.example.com/dev-1"]


def test_subscribe_api_and_settings_page(auth_client, db, user, sent):
    page = auth_client.get("/ustawienia")
    assert "Włącz powiadomienia na tym urządzeniu" in page.text and "Twoje urządzenia" not in page.text

    key = auth_client.get("/push/key").json()["key"]
    assert len(base64.urlsafe_b64decode(key + "==")) == 65  # uncompressed P-256 point

    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "BPk", "auth": "xyz"}}
    ua = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; 23113RKC6G) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
    }
    assert auth_client.post("/push/subscribe", json=sub, headers=ua).json() == {"ok": True}
    auth_client.post("/push/subscribe", json=sub, headers=ua)  # same browser again → still one device
    (dev,) = db.scalars(select(PushSubscription))
    assert dev.user_id == user.id and dev.label == "Android · Chrome"
    assert auth_client.post("/push/subscribe", json={"endpoint": "http://x", "keys": {}}).status_code == 400

    page = auth_client.get("/ustawienia")
    assert "Android · Chrome" in page.text
    r = auth_client.post("/ustawienia/test")
    assert "Wysłano powiadomienie próbne (1 urządzenie)" in r.text
    assert sent[-1]["title"].startswith("Domownik") and sent[-1]["_endpoint"] == sub["endpoint"]

    auth_client.post("/ustawienia", data={"notify_morning": "1"})
    db.refresh(user)
    assert user.notify_morning is True and user.notify_evening is False

    auth_client.post("/push/unsubscribe", json={"endpoint": sub["endpoint"]})
    assert db.scalar(select(PushSubscription)) is None


def test_subscribe_requires_csrf(auth_client):
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "BPk", "auth": "xyz"}}
    assert auth_client.post("/push/subscribe", json=sub, headers={"X-CSRF-Token": "zly"}).status_code == 403


def test_service_worker_served_from_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert 'addEventListener("push"' in r.text


def test_real_encryption_and_vapid(db, user, monkeypatch):
    """pywebpush really encrypts for a browser key and signs with our VAPID key; only HTTP is faked."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    browser = ec.generate_private_key(ec.SECP256R1())
    p256dh = (
        base64.urlsafe_b64encode(
            browser.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        )
        .rstrip(b"=")
        .decode()
    )
    auth = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
    sub = PushSubscription(user_id=user.id, endpoint="https://fcm.googleapis.com/fcm/send/x", p256dh=p256dh, auth=auth)

    captured = {}

    class Resp:
        status_code = 201
        text = ""
        headers: dict = {}

    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        captured.update(url=url, data=data, headers=headers)
        return Resp()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    assert webpush.deliver(sub, json.dumps({"title": "Zażółć"}), urgent=True) is True
    assert captured["url"] == sub.endpoint
    assert captured["headers"]["content-encoding"] == "aes128gcm"
    assert captured["headers"]["Urgency"] == "high"
    assert captured["headers"]["authorization"].startswith("vapid t=")
    assert b"Za" not in captured["data"]  # encrypted
