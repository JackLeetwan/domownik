"""External calendars by ICS link: parsing (recurring, all-day, time zones), refresh, the calendar views,
the "Dziś" card, the push line, privacy and links into the local network."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from domownik import extcal, notify
from domownik.config import get_settings, today
from domownik.models import ExternalCalendar, ExternalEvent, Item, PushSubscription, User
from domownik.security import hash_password

T = date(2026, 11, 3)  # a Tuesday


def _ics(*events: str) -> bytes:
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//PL", *events, "END:VCALENDAR", ""])
    return body.encode()


def _ev(*lines: str) -> str:
    return "\r\n".join(["BEGIN:VEVENT", *lines, "END:VEVENT"])


SAMPLE = _ics(
    # weekly on Thursdays 17:00–18:00 Warsaw, one week skipped, one moved
    _ev(
        "UID:rytmika",
        "DTSTART;TZID=Europe/Warsaw:20261105T170000",
        "DTEND;TZID=Europe/Warsaw:20261105T180000",
        "RRULE:FREQ=WEEKLY;UNTIL=20261126T230000Z",
        "EXDATE;TZID=Europe/Warsaw:20261112T170000",
        "SUMMARY:Rytmika",
        "LOCATION:Sala 2",
    ),
    _ev(
        "UID:rytmika",
        "RECURRENCE-ID;TZID=Europe/Warsaw:20261119T170000",
        "DTSTART;TZID=Europe/Warsaw:20261120T160000",
        "DTEND;TZID=Europe/Warsaw:20261120T170000",
        "SUMMARY:Rytmika (przeniesiona)",
    ),
    # all-day, two days (DTEND is the day after)
    _ev("UID:wolne", "DTSTART;VALUE=DATE:20261111", "DTEND;VALUE=DATE:20261113", "SUMMARY:Przedszkole zamknięte"),
    # in UTC: 16:30Z is 17:30 in Warsaw in November
    _ev("UID:zebranie", "DTSTART:20261103T163000Z", "DTEND:20261103T173000Z", "SUMMARY:Zebranie\\, grupa Misie"),
    _ev("UID:odwolane", "DTSTART;VALUE=DATE:20261104", "STATUS:CANCELLED", "SUMMARY:Odwołane"),
    _ev("UID:dawno", "DTSTART;VALUE=DATE:20240101", "SUMMARY:Dawno temu"),
)


def test_parse_expands_and_converts():
    occs = extcal.parse(SAMPLE, T)
    got = [(o.start, o.title) for o in occs]
    assert got == [
        (datetime(2026, 11, 3, 17, 30), "Zebranie, grupa Misie"),
        (datetime(2026, 11, 5, 17, 0), "Rytmika"),
        (date(2026, 11, 11), "Przedszkole zamknięte"),
        (datetime(2026, 11, 20, 16, 0), "Rytmika (przeniesiona)"),
        (datetime(2026, 11, 26, 17, 0), "Rytmika"),
    ]


def test_parse_rejects_garbage():
    with pytest.raises(extcal.CalendarError):
        extcal.parse(b"<html>nope</html>", T)


def test_urls_and_local_network():
    assert extcal.normalize_url(" webcal://example.com/a.ics ") == "https://example.com/a.ics"
    with pytest.raises(extcal.CalendarError):
        extcal.normalize_url("file:///etc/passwd")
    for host in ("localhost", "127.0.0.1", "192.168.1.10"):
        with pytest.raises(extcal.CalendarError):
            extcal._check_host(host)


def _now() -> datetime:
    return datetime.combine(today(), datetime.min.time()).replace(hour=9, tzinfo=get_settings().tz)


def _shifted(raw: bytes) -> bytes:
    """SAMPLE moved so that 2026-11-03 becomes today (tests run on any date)."""
    import re

    delta = (today() - T).days

    def move(m: re.Match) -> str:
        d = date(int(m[1]), int(m[2]), int(m[3])) + timedelta(days=delta)
        return d.strftime("%Y%m%d")

    text = raw.decode()
    # the UTC meeting in local time (the offset depends on the season of "today")
    text = text.replace("DTSTART:20261103T163000Z", "DTSTART;TZID=Europe/Warsaw:20261103T173000")
    text = text.replace("DTEND:20261103T173000Z", "DTEND;TZID=Europe/Warsaw:20261103T183000")
    return re.sub(r"(\d{4})(\d{2})(\d{2})(?=T|\r|;|$)", move, text).encode()


@pytest.fixture
def cal(db):
    c = ExternalCalendar(name="Przedszkole", url="https://example.com/p.ics", icon="🧸")
    db.add(c)
    db.commit()
    assert extcal.refresh(db, c, _now(), raw=_shifted(SAMPLE)) == 5
    return c


def test_refresh_rows_and_failure_keeps_events(db, cal, monkeypatch):
    t = today()
    rows = db.query(ExternalEvent).order_by(ExternalEvent.starts_on).all()
    closed = next(r for r in rows if r.title == "Przedszkole zamknięte")
    assert closed.starts_on == t + timedelta(days=8) and closed.ends_on == t + timedelta(days=9)
    assert closed.time_label == "cały dzień"
    meeting = next(r for r in rows if r.title.startswith("Zebranie"))
    assert (meeting.starts_on, meeting.starts_at, meeting.ends_at) == (t, "17:30", "18:30")
    by_day = extcal.events_by_day(db, t + timedelta(days=8), t + timedelta(days=9))
    assert [e.title for e in by_day[t + timedelta(days=9)]] == ["Przedszkole zamknięte"]

    def broken(url):
        raise extcal.CalendarError("Serwer kalendarza odpowiedział błędem 500")

    monkeypatch.setattr(extcal, "fetch", broken)
    assert extcal.refresh(db, cal, _now()) == -1
    assert cal.error.endswith("500") and db.query(ExternalEvent).count() == 5


def test_refresh_due_only_stale(db, cal, monkeypatch):
    calls = []
    monkeypatch.setattr(extcal, "fetch", lambda url: calls.append(url) or _shifted(SAMPLE))
    extcal.refresh_due(db, _now() + timedelta(minutes=10))
    assert calls == []
    extcal.refresh_due(db, _now() + timedelta(minutes=61))
    assert calls == ["https://example.com/p.ics"]


def test_views_show_events(auth_client, db, cal):
    db.add(Item(name="Dom", kind="house"))  # the dashboard shows its cards once there is a zone
    db.commit()
    t = today()
    month = auth_client.get(f"/kalendarz?d={t.isoformat()}").text
    assert "Zebranie, grupa Misie" in month and "🔗 Kalendarze z zewnątrz (1)" in month
    week = auth_client.get(f"/kalendarz/tydzien?d={t.isoformat()}").text
    assert "Zebranie, grupa Misie" in week and "17:30–18:30" in week
    day = auth_client.get(f"/kalendarz/dzien?d={(t + timedelta(days=9)).isoformat()}").text
    assert "Przedszkole zamknięte" in day and "Nic na ten dzień" not in day
    assert "Zebranie, grupa Misie" in auth_client.get("/").text  # the "Dziś" card


def test_add_page_and_remove(auth_client, db, monkeypatch):
    monkeypatch.setattr(extcal, "fetch", lambda url: _shifted(SAMPLE))
    page = auth_client.get("/kalendarz/zewnetrzne")
    assert "Tajny adres w formacie iCal" in page.text
    r = auth_client.post(
        "/kalendarz/zewnetrzne",
        data={"name": "Szkoła", "url": "webcal://example.com/secret-token/s.ics", "icon": "🏫", "notify": "1"},
    )
    assert r.status_code == 200 and "Szkoła" in r.text and "5 wydarzeń" in r.text
    assert "secret-token" not in r.text  # the link's secret part is never shown
    cal = db.query(ExternalCalendar).one()
    assert cal.url == "https://example.com/secret-token/s.ics" and cal.icon == "🏫" and cal.notify

    bad = auth_client.post("/kalendarz/zewnetrzne", data={"name": "X", "url": "ftp://example.com/x"})
    assert "https://" in bad.text and db.query(ExternalCalendar).count() == 1

    auth_client.post(f"/kalendarz/zewnetrzne/{cal.id}/powiadomienia")
    db.expire_all()
    assert not db.get(ExternalCalendar, cal.id).notify
    auth_client.post(f"/kalendarz/zewnetrzne/{cal.id}/usun")
    db.expire_all()
    assert db.query(ExternalCalendar).count() == 0 and db.query(ExternalEvent).count() == 0


def test_private_calendar_hidden_from_others(auth_client, db, user, monkeypatch):
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.commit()
    c = ExternalCalendar(name="Praca Oli", url="https://example.com/w.ics", user_id=other.id, private_to_id=other.id)
    db.add(c)
    db.commit()
    extcal.refresh(db, c, _now(), raw=_shifted(SAMPLE))
    page = auth_client.get(f"/kalendarz?d={today().isoformat()}").text
    assert "Zebranie, grupa Misie" not in page and "Praca Oli" not in auth_client.get("/kalendarz/zewnetrzne").text
    assert auth_client.post(f"/kalendarz/zewnetrzne/{c.id}/usun").status_code == 404
    assert extcal.for_push(db, today(), user.id) == []
    assert extcal.for_push(db, today(), other.id)[0].title.startswith("Zebranie")


def test_push_line(db, user, cal):
    db.add(PushSubscription(user_id=user.id, endpoint="https://push.example/1", p256dh="k", auth="a"))
    db.commit()
    (d,) = notify.build_digests(db, "morning", today(), preview_for=user)
    assert any(ln.text == "📅 Dziś: 17:30 Zebranie, grupa Misie" for ln in d.lines)
    cal.notify = False
    db.commit()
    (d,) = notify.build_digests(db, "morning", today(), preview_for=user)
    assert not any(ln.text.startswith("📅") for ln in d.lines)


def test_migration_0026(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn, conn.begin():
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        tables = set(sa.inspect(conn).get_table_names())
        assert {"external_calendar", "external_event"} <= tables
        command.downgrade(cfg, "3d7a9c1e5b26")
        assert "external_calendar" not in set(sa.inspect(conn).get_table_names())
        command.upgrade(cfg, "head")


def test_odd_events_are_skipped_not_fatal():
    raw = _ics(
        _ev("UID:mixed", "DTSTART;VALUE=DATE:20261110", "DTEND:20261111T100000Z", "SUMMARY:Mieszane"),
        _ev(
            "UID:two",
            "DTSTART;VALUE=DATE:20261112",
            "RRULE:FREQ=DAILY;COUNT=2",
            "RRULE:FREQ=WEEKLY;COUNT=2",
            "SUMMARY:Dwie reguły",
        ),
        _ev("UID:ok", "DTSTART;VALUE=DATE:20261113", "SUMMARY:Dobre"),
    )
    titles = [o.title for o in extcal.parse(raw, T)]
    assert "Dobre" in titles and "Dwie reguły" in titles


def test_unexpected_error_is_stored(db, monkeypatch):
    c = ExternalCalendar(name="X", url="https://example.com/x.ics")
    db.add(c)
    db.commit()

    def boom(raw, today):
        raise RuntimeError("bug")

    monkeypatch.setattr(extcal, "parse", boom)
    assert extcal.refresh(db, c, _now(), raw=b"x") == -1
    assert c.error and c.fetched_at is not None
