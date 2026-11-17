"""🔄 /api/grafik: an outside system (the Zdrofit bot) keeps its one-off entries in /grafik in step."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from domownik.config import get_settings, today
from domownik.models import ScheduleEvent

TOKEN = "s" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_token", TOKEN)


def _body(*entries, assignee="Jan", days=3):
    t = today()
    return {
        "assignee": assignee,
        "window_from": t.isoformat(),
        "window_to": (t + timedelta(days=days)).isoformat(),
        "entries": list(entries),
    }


def _entry(key, day_offset=1, start="17:00", title="Pilates Reformer"):
    return {
        "key": key,
        "title": title,
        "date": (today() + timedelta(days=day_offset)).isoformat(),
        "start": start,
        "end": "17:55",
        "notes": "Zdrofit Galeria Malwowa",
    }


def _events(db):
    db.expire_all()
    return list(db.scalars(select(ScheduleEvent).order_by(ScheduleEvent.id)))


def test_needs_the_token(client, monkeypatch):
    assert client.put("/api/grafik/zdrofit-paula", json=_body()).status_code == 401
    assert (
        client.put("/api/grafik/zdrofit-paula", json=_body(), headers={"Authorization": "Bearer zly"}).status_code
        == 401
    )
    monkeypatch.setattr(get_settings(), "sync_token", "")
    assert client.put("/api/grafik/zdrofit-paula", json=_body(), headers=AUTH).status_code == 404


def test_adds_updates_and_removes_cancelled(client, db, user):
    r = client.put("/api/grafik/zdrofit-paula", json=_body(_entry("1"), _entry("2", 2)), headers=AUTH)
    assert r.json() == {"added": 2, "updated": 0, "removed": 0}
    evs = _events(db)
    assert [e.source for e in evs] == ["zdrofit-paula:1", "zdrofit-paula:2"]
    d1 = today() + timedelta(days=1)
    assert evs[0].starts_on == evs[0].ends_on == d1 and evs[0].days == [d1.weekday()]
    assert evs[0].assignee_id == user.id and evs[0].starts_at == "17:00" and evs[0].ends_at == "17:55"

    # same list again: nothing changes; then class 1 moved, class 2 cancelled
    assert client.put("/api/grafik/zdrofit-paula", json=_body(_entry("1"), _entry("2", 2)), headers=AUTH).json() == {
        "added": 0,
        "updated": 0,
        "removed": 0,
    }
    r = client.put("/api/grafik/zdrofit-paula", json=_body(_entry("1", start="16:00")), headers=AUTH)
    assert r.json() == {"added": 0, "updated": 1, "removed": 1}
    evs = _events(db)
    assert [(e.source, e.starts_at) for e in evs] == [("zdrofit-paula:1", "16:00")]


def test_keeps_hand_entries_other_feeds_past_and_outside_window(client, db, user):
    db.add(ScheduleEvent(title="Basen", weekdays="0,2", starts_at="17:00"))
    db.add(ScheduleEvent(title="Stare", weekdays="0", starts_at="10:00", source="zdrofit-paula:old",
                         starts_on=today() - timedelta(days=1), ends_on=today() - timedelta(days=1)))  # fmt: skip
    db.commit()
    client.put("/api/grafik/inny", json=_body(_entry("x")), headers=AUTH)
    client.put("/api/grafik/zdrofit-paula", json=_body(_entry("far", 10), days=20), headers=AUTH)
    r = client.put("/api/grafik/zdrofit-paula", json=_body(), headers=AUTH)  # window: next 3 days
    assert r.json()["removed"] == 0
    assert {e.title if not e.source else e.source for e in _events(db)} == {
        "Basen",
        "zdrofit-paula:old",
        "inny:x",
        "zdrofit-paula:far",
    }


def test_shows_in_the_week_but_not_in_the_fixed_list(client, auth_client, db, user):
    client.put("/api/grafik/zdrofit-paula", json=_body(_entry("1", 0, start="23:59")), headers=AUTH)
    page = auth_client.get("/grafik").text
    assert "Pilates Reformer" in page
    assert "Wszystkie stałe wpisy" not in page
    ev = _events(db)[0]
    assert "Wpis z synchronizacji (zdrofit-paula)" in auth_client.get(f"/grafik/{ev.id}").text


def test_rejects_unknown_person_and_bad_time(client, user):
    assert client.put("/api/grafik/zdrofit-paula", json=_body(assignee="Nikt"), headers=AUTH).status_code == 422
    bad = _entry("1", start="25:00")
    assert client.put("/api/grafik/zdrofit-paula", json=_body(bad), headers=AUTH).status_code in (400, 422)
