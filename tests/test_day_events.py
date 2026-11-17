"""📌 All-day entries of the family calendar: holidays at whose family, trips, school breaks."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from domownik import dayevents, ics, notify
from domownik.config import now, today
from domownik.models import DayEvent, Item, User


def _add(db, title="🎄 Wigilia – Wschowa", starts_on=None, ends_on=None, **kw):
    starts_on = starts_on or today() + timedelta(days=3)
    ev = DayEvent(title=title, starts_on=starts_on, ends_on=ends_on or starts_on, **kw)
    db.add(ev)
    db.commit()
    return ev


def test_by_day_spreads_multi_day_and_repeats_yearly(db):
    trip = _add(db, "Wyjazd", date(2027, 2, 10), date(2027, 2, 13))
    bday = _add(db, "Rocznica", date(2024, 2, 29), yearly=True)
    days = dayevents.by_day(db, date(2027, 2, 1), date(2027, 3, 1))
    assert [d for d, occs in sorted(days.items()) if any(o.event is trip for o in occs)] == [
        date(2027, 2, 10),
        date(2027, 2, 11),
        date(2027, 2, 12),
        date(2027, 2, 13),
    ]
    # 29 February in a common year → 28 February
    assert [o.event for o in days[date(2027, 2, 28)]] == [bday]
    assert days[date(2027, 2, 11)][0].multi and days[date(2027, 2, 11)][0].starts_on == date(2027, 2, 10)
    # yearly: not before the year it was added
    assert dayevents.by_day(db, date(2023, 2, 1), date(2023, 3, 1)) == {}


def test_mine_filters_by_person(db, user):
    other = User(email="ola@example.com", name="Ola", password_hash="x")
    db.add(other)
    db.commit()
    d = today() + timedelta(days=1)
    _add(db, "Wszyscy", d)
    _add(db, "Jana", d, user_id=user.id)
    _add(db, "Oli", d, user_id=other.id)
    assert {o.title for o in dayevents.on(db, d, user_id=user.id)} == {"Wszyscy", "Jana"}
    assert len(dayevents.on(db, d)) == 3


def test_add_edit_delete_through_the_form(auth_client, db):
    d = today() + timedelta(days=5)
    r = auth_client.post(
        "/kalendarz/dni",
        data={
            "title": "Ferie",
            "icon": "🏖️",
            "starts_on": d.isoformat(),
            "ends_on": (d + timedelta(days=4)).isoformat(),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    ev = db.scalar(select(DayEvent))
    assert (ev.title, ev.icon, ev.length, ev.yearly, ev.notify) == ("Ferie", "🏖️", 5, False, False)

    page = auth_client.get(f"/kalendarz?m={d.strftime('%Y-%m')}&d={d.isoformat()}")
    assert "🏖️ Ferie" in page.text and f"/kalendarz/dni/{ev.id}" in page.text
    assert "Ferie" in auth_client.get("/kalendarz/dni").text
    assert "Ferie" in auth_client.get(f"/kalendarz/tydzien?d={d.isoformat()}").text
    assert "Ferie" in auth_client.get(f"/kalendarz/dzien?d={(d + timedelta(days=2)).isoformat()}").text
    assert 'value="Ferie"' in auth_client.get(f"/kalendarz/dni/{ev.id}").text

    r = auth_client.post(
        f"/kalendarz/dni/{ev.id}",
        data={"title": "Ferie zimowe", "starts_on": d.isoformat(), "yearly": "1", "notify": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    ev = db.get(DayEvent, ev.id)
    assert (ev.title, ev.icon, ev.length, ev.yearly, ev.notify) == ("Ferie zimowe", "📌", 1, True, True)

    assert auth_client.post(f"/kalendarz/dni/{ev.id}/usun", follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.scalar(select(DayEvent)) is None


def test_form_errors(auth_client, db):
    d = today()
    for data, msg in (
        ({"title": "", "starts_on": d.isoformat()}, "Podaj nazwę"),
        ({"title": "X"}, "Podaj dzień"),
        ({"title": "X", "starts_on": d.isoformat(), "ends_on": (d - timedelta(days=1)).isoformat()}, "wcześniejszy"),
        ({"title": "X", "starts_on": d.isoformat(), "ends_on": (d + timedelta(days=100)).isoformat()}, "Najwyżej"),
    ):
        r = auth_client.post("/kalendarz/dni", data=data)
        assert r.status_code == 422 and msg in r.text
    assert db.scalar(select(DayEvent)) is None


def test_quick_add(auth_client, db):
    d = today() + timedelta(days=2)
    menu = auth_client.get(f"/dodaj?d={d.isoformat()}")
    assert "/dodaj/day" in menu.text
    assert 'hx-post="/kalendarz/dni"' in auth_client.get(f"/dodaj/day?d={d.isoformat()}").text
    r = auth_client.post(
        "/kalendarz/dni",
        data={"title": "Wigilia", "starts_on": d.isoformat(), "notify": "1"},
        headers={"X-Quick-Add": "1"},
    )
    assert r.status_code == 200 and "dm-added" in r.headers["HX-Trigger"]
    r = auth_client.post("/kalendarz/dni", data={"title": ""}, headers={"X-Quick-Add": "1"})
    assert r.status_code == 422 and r.headers["HX-Retarget"] == "#qa-error"


def test_dashboard_today_card(auth_client, db):
    db.add(Item(name="Dom", kind="house"))
    _add(db, "Wigilia – Wschowa", today(), icon="🎄")
    assert "🎄 Wigilia – Wschowa" in auth_client.get("/").text


def test_push_lines(db, user):
    _add(db, "Wigilia – Wschowa", today(), icon="🎄")
    _add(db, "Cicho", today(), notify=False)
    _add(db, "Boże Narodzenie – Palędzie", today() + timedelta(days=1), icon="🎄")
    texts = [ln.text for d in notify.build_digests(db, "morning", today(), preview_for=user) for ln in d.lines]
    assert "📌 Dziś: 🎄 Wigilia – Wschowa" in texts
    texts = [ln.text for d in notify.build_digests(db, "evening", today(), preview_for=user) for ln in d.lines]
    assert "📌 Jutro: 🎄 Boże Narodzenie – Palędzie" in texts


def test_week_text(db, user):
    sunday = today() + timedelta(days=(6 - today().weekday()))
    _add(db, "Wyjazd", sunday + timedelta(days=2), sunday + timedelta(days=4))
    text = notify.week_text(db, user, [], sunday)
    assert text and "📌 wt Wyjazd" in text


def test_ics_feed(db, user):
    _add(db, "Wyjazd", today() + timedelta(days=2), today() + timedelta(days=3), icon="✈️")
    feed = ics.build(db, user, today(), now())
    first = today() + timedelta(days=2)
    assert f"DTSTART;VALUE=DATE:{first.strftime('%Y%m%d')}" in feed
    assert f"DTEND;VALUE=DATE:{(first + timedelta(days=2)).strftime('%Y%m%d')}" in feed
    assert "SUMMARY:✈️ Wyjazd" in feed
