"""✉️ Z poczty: dates out of kindergarten emails (domain.mailparse), the read-only mailbox check (mailwatch)
and confirming them in 📥 Do rozpoznania (routers/mail)."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from domownik import mailwatch
from domownik.config import get_settings
from domownik.domain import mailparse
from domownik.models import DayEvent, Item, MailMessage, MailState, ShoppingItem, Task

SENDER = "teacher@example.com"

# the formats seen in real emails of the kindergarten (names changed)
DECEMBER = """Dzień dobry,

W piątek tj. 27. listopad odbędzie się nasza wycieczka do teatru. Ponieważ wyjazd zaplanowany jest o godzinie 8.30, zatem śniadanie zaczniemy jeść o godzinie 8. Proszę aby o godzinie 8.20 wszystkie dzieci już były w przedszkolu. Powrót zaplanowany na obiad. W teatrze obowiązuje strój galowy.

W grudniu :

03.12 - pieczenie pierników ( ciasto oraz foremki do wycinania przyniesie mama jednego z dzieci za co serdecznie dziękujemy)

04.12 - ozdabianie pierników ( w ten dzień dzieci mogą przynieść posypki, pisaki i będzie nam potrzebny cukier puder aby zrobić lukier)

08.12 - wycieczka do fabryki bombek

21.12 o godz. 11.00 Odbędzie się przedstawienie świąteczne, na które zapraszamy wszystkich rodziców :)

Pozdrawiam serdecznie
"""
PHOTOS = """Dzień dobry,

Poniżej link do ankiety sprawdzającej obecność dzieci w przerwie między świątecznej. Poproszę o wypełnienie.

26 listopada tj . czwartek odbędzie się sesja zdjęciowa dla naszej grupy.

Pozdrawiam serdecznie
"""
TOMORROW = "Dzień dobry,\n\nProszę aby ze względu na środowy Dzień Niepodległości dzieci przyszły jutro ubrane na galowo. Dziękuję.\n"
ABSENCE = "Dzień dobry,\nChciałabym dać znać, że nie będzie mnie od czwartku do poniedziałku włącznie w pracy.\n"
FAIRY = "Dzień Postaci z Bajek\n\nJuż 5 listopada nasze przedszkole zamieni się w Zaczarowany Las. Prosimy o przebranie dzieci.\n"


def _p(subject, body, sent):
    res = mailparse.parse(subject, body, sent)
    return [(p.day, p.end, p.at, p.title) for p in res.proposals], res


# ---------------------------------------------------------------- the parser


def test_december_email_every_date_with_title_and_hour():
    rows, res = _p("Grudzień", DECEMBER, date(2026, 11, 24))
    assert rows == [
        (date(2026, 11, 27), date(2026, 11, 27), None, "Wycieczka do teatru"),
        (date(2026, 12, 3), date(2026, 12, 3), None, "Pieczenie pierników"),
        (date(2026, 12, 4), date(2026, 12, 4), None, "Ozdabianie pierników"),
        (date(2026, 12, 8), date(2026, 12, 8), None, "Wycieczka do fabryki bombek"),
        (date(2026, 12, 21), date(2026, 12, 21), time(11, 0), "Przedstawienie świąteczne"),
    ]
    assert res.unread == []
    assert all(not p.warnings for p in res.proposals)  # the weekday "piątek" matches 27.11.2026
    theatre = res.proposals[0]
    assert "strój galowy" in theatre.paragraph and "8.20" in theatre.paragraph  # the note keeps the details
    assert theatre.icon == "🎭"
    assert res.proposals[2].bring == ["posypki", "pisaki", "cukier puder"]
    assert res.proposals[1].bring == []  # "przyniesie mama …" is no request to parents


def test_photo_session_with_weekday():
    rows, res = _p("informacje", PHOTOS, date(2026, 11, 18))
    assert rows == [(date(2026, 11, 26), date(2026, 11, 26), None, "Sesja zdjęciowa dla grupy")]
    assert res.proposals[0].icon == "📸" and not res.proposals[0].warnings


def test_tomorrow_is_the_day_after_sending_and_a_request_gets_a_short_title():
    rows, res = _p("dzień jutrzejszy", TOMORROW, date(2026, 11, 9))
    assert rows == [(date(2026, 11, 10), date(2026, 11, 10), None, "Strój galowy")]
    assert not res.proposals[0].warnings  # "środowy" is an adjective, not a weekday to check


def test_weekday_without_date_is_shown_to_read_not_guessed():
    rows, res = _p("moja nieobecność", ABSENCE, date(2026, 11, 17))
    assert rows == []
    assert len(res.unread) == 1 and "czwartku" in res.unread[0]


def test_short_heading_names_the_only_day():
    rows, res = _p("5 listopad", FAIRY, date(2026, 10, 23))
    assert rows == [(date(2026, 11, 5), date(2026, 11, 5), None, "Dzień Postaci z Bajek")]
    assert res.proposals[0].icon == "🎉"


def test_wrong_weekday_is_flagged_and_unticked():
    _rows, res = _p("x", "W czwartek 27.11 idziemy do kina.", date(2026, 11, 20))
    (p,) = res.proposals
    assert p.day == date(2026, 11, 27) and any("piątek" in w for w in p.warnings) and not p.checked


@pytest.mark.parametrize(
    "text",
    ["Dyżur 28–30.12.", "Dyżur od 28 do 30 grudnia.", "Dyżur 28, 29 i 30 grudnia.", "Dyżur 28-30 grudnia."],
)
def test_ranges_become_one_entry(text):
    rows, _ = _p("x", text, date(2026, 11, 18))
    assert [(r[0], r[1]) for r in rows] == [(date(2026, 12, 28), date(2026, 12, 30))]


def test_list_of_days_that_are_not_consecutive_gives_one_each():
    rows, _ = _p("x", "Zajęcia z rytmiki 2, 9 i 16 grudnia.", date(2026, 11, 18))
    assert [r[0] for r in rows] == [date(2026, 12, 2), date(2026, 12, 9), date(2026, 12, 16)]


def test_january_in_a_december_email_is_next_year_and_explicit_years_win():
    rows, _ = _p("x", "Zebranie 05.01 o 17:00.", date(2026, 12, 15))
    assert rows[0][0] == date(2027, 1, 5) and rows[0][2] == time(17, 0)
    rows, _ = _p("x", "Bal 14.02.2027.", date(2026, 12, 15))
    assert rows[0][0] == date(2027, 2, 14)


def test_times_are_not_dates():
    rows, res = _p("x", "Prosimy przyprowadzić dzieci o godz. 8.30 i odebrać o 15.15.", date(2026, 11, 18))
    assert rows == [] and res.unread == []


def test_impossible_date_is_shown_to_read():
    rows, res = _p("x", "Spotkanie 31.02 w sali.", date(2026, 1, 10))
    assert rows == [] and res.unread == ["Spotkanie 31.02 w sali."]


def test_past_and_far_dates_are_flagged():
    _rows, res = _p("x", "Dziękujemy za udział w balu 10.11.", date(2026, 11, 20))
    assert any("przed wysłaniem" in w for w in res.proposals[0].warnings)
    _rows, res = _p("x", "Wycieczka 10.11.2028.", date(2026, 11, 20))
    assert any("daleko" in w for w in res.proposals[0].warnings)


def test_quoted_reply_and_signature_are_ignored():
    body = "Dziękuję za informację.\n\nW dniu 9.11.2026 o 08:16 ktoś napisał:\n> Jutro galowo.\n> 12.11 bal\n"
    rows, res = _p("Re: x", body, date(2026, 11, 9))
    assert rows == [] and res.unread == []


def test_subject_date_used_only_when_the_body_has_none():
    rows, _ = _p("Zebranie 3 grudnia", "Zapraszamy wszystkich rodziców.", date(2026, 11, 20))
    assert rows == [(date(2026, 12, 3), date(2026, 12, 3), None, "Zebranie 3 grudnia")]


def test_parser_never_breaks_on_any_text():
    rnd = random.Random(7)
    alphabet = "0123456789 .,:-–/()\nabcdefghijklmnoprstuwyząćęłńóśźżABCDĘŁ"
    words = [
        "jutro",
        "piątek",
        "grudnia",
        "listopad",
        "godz.",
        "o",
        "tj.",
        "31.02",
        "28–30.12",
        "przynieść",
        "potrzebny",
    ]
    for _ in range(1500):
        text = "".join(
            rnd.choice(alphabet) if rnd.random() < 0.8 else " " + rnd.choice(words) + " "
            for _ in range(rnd.randint(0, 200))
        )
        res = mailparse.parse(text[:20], text, date(2026, rnd.randint(1, 12), rnd.randint(1, 28)))
        for p in res.proposals:
            assert p.end >= p.day and p.title


# ---------------------------------------------------------------- reading emails


@pytest.fixture
def mail_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mail_user", "me@example.com")
    monkeypatch.setattr(s, "mail_password", "app-password")
    monkeypatch.setattr(s, "mail_from", f"{SENDER}, other@example.com")
    monkeypatch.setattr(s, "mail_check_at", "18:30")
    return s


def raw_email(body: str, *, subject="Grudzień", sender=SENDER, mid="<1@example.com>", html=False, when=None) -> bytes:
    m = EmailMessage()
    m["From"] = f"Pani Nauczycielka <{sender}>"
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = when or "Tue, 24 Nov 2026 14:40:00 +0100"
    if mid:
        m["Message-ID"] = mid
    if html:
        m.set_content(
            "<html><head><style>p{}</style></head><body><p>" + body.replace("\n\n", "</p><p>") + "</p></body></html>",
            subtype="html",
        )
    else:
        m.set_content(body, cte="quoted-printable")
    return m.as_bytes()


def test_read_message_plain_html_and_foreign_sender(mail_on):
    p = mailwatch.read_message(raw_email(DECEMBER))
    assert p and p.sender == SENDER and p.subject == "Grudzień" and "pierników" in p.body
    assert p.sent_at == datetime(2026, 11, 24, 14, 40)
    h = mailwatch.read_message(raw_email(DECEMBER, html=True))
    assert h and "03.12 - pieczenie pierników" in h.body and "p{}" not in h.body
    assert [x.day for x in mailparse.parse(h.subject, h.body, h.sent_at.date()).proposals] == [
        x.day for x in mailparse.parse(p.subject, p.body, p.sent_at.date()).proposals
    ]
    assert mailwatch.read_message(raw_email("x", sender="teacher@example.com.evil.org")) is None
    nomid = mailwatch.read_message(raw_email("x", mid=None))
    assert nomid and nomid.message_id.startswith("sha256:")


def test_check_keeps_new_emails_once_and_looks_back(db, user, mail_on):
    seen = []

    def fetch(since):
        seen.append(since)
        return [
            raw_email(DECEMBER),
            raw_email(PHOTOS, subject="informacje", mid="<2@example.com>"),
            raw_email("x", sender="spam@example.com", mid="<3@x>"),
        ]

    t = datetime(2026, 11, 27, 18, 31)
    assert mailwatch.check(db, t, fetch=fetch) == 2
    assert mailwatch.check(db, t + timedelta(days=1), fetch=fetch) == 0  # Message-ID: never twice
    assert seen == [date(2026, 11, 13), date(2026, 11, 20)]  # 14 days the first time, then a week before the last good
    assert mailwatch.count(db) == 2
    st = db.get(MailState, 1)
    assert st.error is None and st.last_ok_at == datetime(2026, 11, 28, 18, 31)


def test_failed_check_is_recorded_pushed_once_a_day_and_warned(db, user, mail_on, monkeypatch):
    pushes = []
    monkeypatch.setattr("domownik.notify.send", lambda db, u, body: pushes.append(body["title"]) or 1)

    def broken(since):
        raise mailwatch.MailError("Poczta odrzuciła logowanie")

    t = datetime(2026, 11, 27, 18, 31)
    assert mailwatch.check(db, t, fetch=broken) == 0
    assert mailwatch.check(db, t + timedelta(hours=1), fetch=broken) == 0
    st = db.get(MailState, 1)
    assert st.error == "Poczta odrzuciła logowanie" and st.last_ok_at is None
    assert len(pushes) == 1  # one push a day, not one an hour
    assert "nie może sprawdzić poczty" in mailwatch.warning(db, t)

    def bug(since):
        raise KeyError("x")

    mailwatch.check(db, t + timedelta(hours=2), fetch=bug)
    assert "Nieoczekiwany błąd" in db.get(MailState, 1).error


def test_new_emails_are_pushed(db, user, mail_on, monkeypatch):
    pushes = []
    monkeypatch.setattr("domownik.notify.send", lambda db, u, body: pushes.append(body) or 1)
    mailwatch.check(db, datetime(2026, 11, 27, 18, 31), fetch=lambda since: [raw_email(DECEMBER)])
    assert len(pushes) == 1 and "1 nowy mail" in pushes[0]["title"] and "5 terminów" in pushes[0]["body"]


def test_due_once_a_day_with_hourly_retry(db, mail_on):
    day = date(2026, 11, 27)
    assert not mailwatch.due(db, datetime.combine(day, time(18, 0)))
    assert mailwatch.due(db, datetime.combine(day, time(18, 30)))
    st = mailwatch.state(db)
    st.last_run_at = st.last_ok_at = datetime.combine(day, time(18, 30))
    assert not mailwatch.due(db, datetime.combine(day, time(23, 0)))
    assert mailwatch.due(db, datetime.combine(day + timedelta(days=1), time(18, 35)))
    st.last_run_at, st.error = datetime.combine(day + timedelta(days=1), time(18, 35)), "x"
    assert not mailwatch.due(db, datetime.combine(day + timedelta(days=1), time(19, 0)))
    assert mailwatch.due(db, datetime.combine(day + timedelta(days=1), time(19, 36)))
    # a phone that was off for the evening catches up the next morning
    st.error = None
    st.last_ok_at = datetime.combine(day - timedelta(days=1), time(18, 30))
    st.last_run_at = st.last_ok_at
    assert mailwatch.due(db, datetime.combine(day, time(18, 31)))


def test_off_without_settings(db):
    assert not mailwatch.configured()
    assert not mailwatch.due(db, datetime(2026, 11, 27, 20, 0))
    assert mailwatch.warning(db, datetime(2026, 11, 27, 20, 0)) is None


def test_old_successful_check_warns_on_dashboard(db, mail_on):
    st = mailwatch.state(db)
    st.last_ok_at = datetime(2026, 11, 24, 18, 30)
    assert mailwatch.warning(db, datetime(2026, 11, 25, 20, 0)) is None
    assert "od 3 dni" in mailwatch.warning(db, datetime(2026, 11, 27, 20, 0))


class FakeIMAP:
    """Records every command – proves the mailbox is opened read-only and messages are only peeked."""

    calls: list = []
    messages: dict = {}

    def __init__(self, host, timeout=None):
        FakeIMAP.calls.append(("connect", host))

    def login(self, user, password):
        FakeIMAP.calls.append(("login", user))

    def select(self, folder, readonly=False):
        FakeIMAP.calls.append(("select", folder, readonly))
        return "OK", [b"2"]

    def search(self, charset, *criteria):
        FakeIMAP.calls.append(("search", *criteria))
        return "OK", [b" ".join(k for k, v in FakeIMAP.messages.items() if v[0] == criteria[1].strip('"'))]

    def fetch(self, num, what):
        FakeIMAP.calls.append(("fetch", num, what))
        return "OK", [(b"1 (BODY[] {10}", FakeIMAP.messages[num][1]), b")"]

    def store(self, *a):  # pragma: no cover – must never be called
        raise AssertionError("the mailbox must not be changed")

    def logout(self):
        FakeIMAP.calls.append(("logout",))


def test_fetch_is_read_only(mail_on, monkeypatch):
    FakeIMAP.calls = []
    FakeIMAP.messages = {b"1": (SENDER, raw_email(DECEMBER)), b"2": ("other@example.com", raw_email("x", mid="<9@x>"))}
    monkeypatch.setattr(mailwatch.imaplib, "IMAP4_SSL", FakeIMAP)
    raws = mailwatch.fetch_raw(date(2026, 11, 20))
    assert len(raws) == 2
    assert ("select", '"INBOX"', True) in FakeIMAP.calls
    assert all(c[2] == "(BODY.PEEK[])" for c in FakeIMAP.calls if c[0] == "fetch")
    assert ("search", "FROM", f'"{SENDER}"', "SINCE", "20-Nov-2026") in FakeIMAP.calls
    assert FakeIMAP.calls[-1] == ("logout",)


def test_login_refused_is_a_readable_error(mail_on, monkeypatch):
    class Refuse(FakeIMAP):
        def login(self, user, password):
            raise mailwatch.imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    monkeypatch.setattr(mailwatch.imaplib, "IMAP4_SSL", Refuse)
    with pytest.raises(mailwatch.MailError, match="hasło aplikacji"):
        mailwatch.fetch_raw(date(2026, 11, 20))


# ---------------------------------------------------------------- confirming in 📥 Do rozpoznania


def _waiting(db, body=DECEMBER, subject="Grudzień"):
    m = MailMessage(
        message_id=f"<{subject}@x>", sender=SENDER, subject=subject, sent_at=datetime(2026, 11, 24, 14, 40), body=body
    )
    db.add(m)
    db.commit()
    return m


def test_page_shows_proposals_and_saves_the_ticked_ones(auth_client, db, user, mail_on):
    child = Item(name="Dziecko", kind="child")
    db.add(child)
    db.commit()
    m = _waiting(db)
    page = auth_client.get("/zdjecia")
    assert page.status_code == 200
    assert "Wycieczka do teatru" in page.text and "Przedstawienie świąteczne" in page.text
    assert 'value="posypki, pisaki, cukier puder"' in page.text
    assert f'<option value="{child.id}" selected' in page.text
    form = {
        "item_id": str(child.id),
        "shopping": "posypki, cukier puder",
        "extra_title": "Mikołajki w grupie",
        "extra_day": "2026-12-07",
    }
    for i in (0, 1, 4):  # theatre, gingerbread, the show at 11:00; 4.12 and 8.12 left out
        form |= {f"pick_{i}": "1"}
    res = mailwatch.proposals(db, m)
    for i, p in enumerate(res.proposals):
        form |= {
            f"title_{i}": p.title,
            f"day_{i}": p.day.isoformat(),
            f"at_{i}": p.at.strftime("%H:%M") if p.at else "",
            f"icon_{i}": p.icon,
        }
    form["title_1"] = "Pieczenie pierników (przedszkole)"
    r = auth_client.post(f"/poczta/{m.id}", data=form, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    evs = {e.title: e for e in db.scalars(select(DayEvent))}
    assert set(evs) == {"Wycieczka do teatru", "Pieczenie pierników (przedszkole)", "Mikołajki w grupie"}
    assert (
        evs["Wycieczka do teatru"].starts_on == date(2026, 11, 27)
        and "strój galowy" in evs["Wycieczka do teatru"].notes
    )
    assert "Z maila „Grudzień”" in evs["Wycieczka do teatru"].notes
    (task,) = db.scalars(select(Task))
    assert (task.title, task.next_due_date, task.due_time, task.item_id) == (
        "Przedstawienie świąteczne",
        date(2026, 12, 21),
        "11:00",
        child.id,
    )
    assert task.remind_minutes == 60
    assert {s.name.lower() for s in db.scalars(select(ShoppingItem))} == {"posypki", "cukier puder"}
    assert db.get(MailMessage, m.id).status == "done"
    assert "Wycieczka do teatru" not in auth_client.get("/zdjecia").text  # gone from the inbox


def test_already_in_domownik_is_flagged(db, user, mail_on):
    db.add(DayEvent(title="Sesja zdjęciowa w przedszkolu", starts_on=date(2026, 11, 26), ends_on=date(2026, 11, 26)))
    m = _waiting(db, PHOTOS, "informacje")
    (p,) = mailwatch.proposals(db, m).proposals
    assert any("już jest" in w for w in p.warnings) and not p.checked


def test_nothing_ticked_is_refused_and_dismiss_hides(auth_client, db, user, mail_on):
    m = _waiting(db, ABSENCE, "moja nieobecność")
    page = auth_client.get("/zdjecia")
    assert "nie umiem zamienić" in page.text and "czwartku" in page.text
    r = auth_client.post(f"/poczta/{m.id}", data={})
    assert r.status_code == 400 and "Nic nie zaznaczone" in r.text
    assert auth_client.post(f"/poczta/{m.id}/odrzuc", follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(MailMessage, m.id).status == "dismissed"
    assert auth_client.post(f"/poczta/{m.id}/odrzuc").status_code == 404


def test_timed_entry_needs_a_target(auth_client, db, user, mail_on):
    m = _waiting(db)
    r = auth_client.post(
        f"/poczta/{m.id}", data={"pick_4": "1", "title_4": "Przedstawienie", "day_4": "2026-12-21", "at_4": "11:00"}
    )
    assert r.status_code == 400 and "Wybierz" in r.text
    db.expire_all()
    assert db.get(MailMessage, m.id).status == "new"


def test_check_now_without_settings(auth_client):
    r = auth_client.post("/poczta/sprawdz")
    assert r.status_code == 400 and "nie jest włączone" in r.text


def test_waiting_email_is_outlined_on_dashboard_and_in_menu(auth_client, db, user):
    db.add(Item(name="Dom", kind="zone"))
    db.commit()
    page = auth_client.get("/").text
    assert "inbox-card" not in page and 'class="attn"' not in page  # nothing waits – nothing shouts
    m = _waiting(db)
    page = auth_client.get("/").text
    assert 'class="inbox-card" href="/zdjecia#poczta"' in page and "✉️ Grudzień" in page
    assert 'class="attn"' in page and 'aria-label="Więcej – 1 do rozpoznania"' in page
    auth_client.post(f"/poczta/{m.id}/odrzuc")
    assert "inbox-card" not in auth_client.get("/").text
