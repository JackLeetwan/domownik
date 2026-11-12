"""Security: what an outsider, a logged-in household member and a hostile request can and cannot do.

- every route needs a login and every change a CSRF token (checked over the whole route table, so a new
  route cannot forget it);
- garbage in any form never gives a 500;
- someone else's private task stays hidden down to its steps, completions and files;
- login: rate limit that cannot be dodged by retyping the e-mail, no timing hint which accounts exist;
- headers (framing, sniffing), cookie flags, escaped error messages, safe redirects, push endpoints
  limited to the browsers' push services, nothing big read before login.
"""

from __future__ import annotations

import io
import re
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from domownik import documents, notify, security, webpush
from domownik.config import get_settings, today
from domownik.main import create_app
from domownik.models import (
    Attachment,
    BoardNote,
    BudgetCategory,
    Chore,
    Comment,
    Completion,
    Contact,
    ExternalCalendar,
    Item,
    Meter,
    Recipe,
    RecurringIncome,
    ScheduleEvent,
    ShoppingItem,
    Task,
    TaskStep,
    Todo,
    User,
)
from domownik.security import hash_password
from tests.conftest import csrf_of

HX = {"HX-Request": "true"}

# reachable without a session – each one authorises in its own way (or has nothing to protect)
PUBLIC = {
    ("GET", "/login"),
    ("POST", "/login"),
    ("POST", "/logout"),
    ("GET", "/healthz"),
    ("GET", "/sw.js"),
    ("GET", "/kuchnia"),  # a bare redirect to /kuchnia/jadlospis, which needs a login
    ("GET", "/ics/{token}.ics"),  # the personal token in the URL (calendar apps cannot log in)
    ("GET", "/n/done/{token}"),  # does nothing on GET
    ("POST", "/n/done/{token}"),  # signed token from the push notification (the service worker has no session)
    ("GET", "/n/timer/{token}/{action}"),  # does nothing on GET
    ("POST", "/n/timer/{token}/{action}"),  # timer buttons in the push – signed token, like /n/done
    ("GET", "/n/supl/{token}"),  # does nothing on GET
    ("POST", "/n/supl/{token}"),  # "✓ Wzięte" in the supplement push – signed token
    # the document reader on the PC (domownik.ocr): Bearer DOMOWNIK_OCR_TOKEN, off when no token is set
    ("POST", "/api/ocr/next"),
    ("GET", "/api/ocr/{job_id}/plik"),
    ("POST", "/api/ocr/{job_id}/wynik"),
}
NO_CSRF = {
    ("POST", "/n/done/{token}"),
    ("POST", "/n/timer/{token}/{action}"),
    ("POST", "/n/supl/{token}"),
    # a Bearer token in a header – a browser never sends it on its own, so there is nothing to forge
    ("POST", "/api/ocr/next"),
    ("POST", "/api/ocr/{job_id}/wynik"),
    # Android's share sheet posts here (it cannot know our token): a SameSite=Lax session never comes along from
    # another site, a foreign Origin is refused, a text is only shown and files only land in 📥 Do rozpoznania
    ("POST", "/udostepnij"),
}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")


def _routes(app) -> list[tuple[str, str, set[str]]]:
    """(method, path, names of all dependencies) for every endpoint, routers included."""

    def walk(routes):
        for r in routes:
            if hasattr(r, "dependant"):
                yield r
            elif hasattr(r, "original_router"):
                yield from walk(r.original_router.routes)
            elif hasattr(r, "routes") and not hasattr(r, "app"):
                yield from walk(r.routes)

    def deps(d, acc: set[str]) -> set[str]:
        for s in d.dependencies:
            acc.add(getattr(s.call, "__name__", ""))
            deps(s, acc)
        return acc

    out = [(m, r.path, deps(r.dependant, set())) for r in walk(app.router.routes) for m in r.methods if m != "HEAD"]
    assert len(out) > 150, "route walk broke – the checks below would pass on nothing"
    return out


def _url(path: str, value: str = "1") -> str:
    return re.sub(r"\{[a-z_]+(?::[a-z]+)?\}", value, path)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 40, 40)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def other(db) -> User:
    u = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


# ---------------------------------------------------------------- the route table


def test_every_route_needs_login_and_csrf():
    missing_login, missing_csrf = [], []
    for method, path, names in _routes(create_app()):
        if (method, path) not in PUBLIC and "current_user" not in names:
            missing_login.append(f"{method} {path}")
        if method != "GET" and (method, path) not in NO_CSRF and "verify_csrf" not in names:
            missing_csrf.append(f"{method} {path}")
    assert missing_login == [] and missing_csrf == []


def test_anonymous_gets_login_page_everywhere(client, db, user):
    db.add(Item(name="Dom", kind="house"))
    db.commit()
    for method, path, _ in _routes(client.app):
        if (method, path) in PUBLIC:
            continue
        url = _url(path)
        r = client.request(method, url, follow_redirects=False)
        if method != "GET" and r.status_code == 403:
            continue  # no CSRF token without a session – refused before the login check
        if path.startswith("/api/"):
            assert r.status_code == 401, f"{method} {url}: {r.status_code}"
            continue
        assert r.status_code == 303 and r.headers["location"] == "/login", f"{method} {url}: {r.status_code}"
        r = client.request(method, url, headers=HX, follow_redirects=False)
        assert r.status_code == 401 and r.headers.get("hx-redirect") == "/login", f"{method} {url} (htmx)"


def test_changes_without_csrf_token_are_refused(auth_client, db):
    db.add(Item(name="Dom", kind="house"))
    db.commit()
    del auth_client.headers["X-CSRF-Token"]
    for method, path, _ in _routes(auth_client.app):
        if method == "GET" or (method, path) in NO_CSRF:
            continue
        url = _url(path)
        for headers in ({}, {"X-CSRF-Token": "zly-token"}):
            r = auth_client.request(method, url, data={"csrf": "zly-token", "name": "x"}, headers=headers)
            assert r.status_code == 403, f"{method} {url}: {r.status_code}"
    assert auth_client.get("/").status_code == 200  # still logged in – nothing was done, nothing broke


# ---------------------------------------------------------------- garbage in → 4xx, never 500

GARBAGE = ["x<'\">", "-1", "99999999999999999999999", "2026-02-30", "", "25:99", "nan"]


def _seed(db, user: User) -> None:
    """One of the common things, so that "/…/1/…" routes reach their form parsing."""
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.flush()
    cat = BudgetCategory(name="Rachunki", kind="planned")
    db.add(cat)
    db.flush()
    t = Task(
        title="Przegląd",
        item_id=house.id,
        category="service",
        recurrence={"mode": "interval", "every": 1, "unit": "year"},
        next_due_date=today() + timedelta(days=5),
        amount_gr=10000,
        direction="expense",
        budget_category_id=cat.id,
    )
    db.add_all(
        [
            Item(name="Ola", kind="member", parent_id=house.id),
            t,
            BoardNote(body="Kod do furtki", created_by_id=user.id),
            Todo(title="Wymienić żarówkę", item_id=house.id),
            ShoppingItem(name="Mleko"),
            Contact(name="Hydraulik"),
            Recipe(name="Naleśniki"),
            Chore(name="Odkurzanie"),
            Meter(item_id=house.id, name="Prąd"),
            ScheduleEvent(title="Basen", weekdays="1", starts_at="17:00"),
            RecurringIncome(name="Pensja", amount_gr=500000, starts_on=today().replace(day=1)),
            ExternalCalendar(name="Szkoła", url="https://example.com/s.ics"),
        ]
    )
    db.flush()
    db.add_all([TaskStep(task_id=t.id, title="Krok"), Completion(task_id=t.id, done_on=today(), due_date=today())])
    db.commit()


def test_garbage_input_never_500(auth_client, db, user, monkeypatch):
    from domownik import extcal

    def offline(url):
        raise extcal.CalendarError("offline")

    monkeypatch.setattr(extcal, "fetch", offline)
    _seed(db, user)
    client = TestClient(auth_client.app, raise_server_exceptions=False, cookies=auth_client.cookies)
    client.headers.update(auth_client.headers)
    # every field name any form has – each route picks the ones it reads
    html = "".join(t.read_text() for t in _templates_dir().rglob("*.html"))
    fields = sorted(set(re.findall(r'name="([a-z_0-9]+)"', html)) - {"csrf"})
    params = ("d", "m", "q", "month", "who", "dl", "k", "scope", "next", "ctx")
    routes = sorted(_routes(client.app), key=lambda r: bool(re.search(r"usun|delete|logout", r[1])))
    errors = []
    for method, path, _ in routes:
        if path == "/logout":
            continue
        for value in GARBAGE:
            url = _url(path, "1" if value != "-1" else "999999")
            if method == "GET":
                r = client.get(url, params=dict.fromkeys(params, value), follow_redirects=False)
            else:
                data = dict.fromkeys(fields, value)
                data.update(
                    {f"{f}_{i}": value for f in ("amount", "d", "due", "title", "c", "p", "pick") for i in (0, 1)}
                )
                r = client.request(method, url, data=data, headers=HX, follow_redirects=False)
            if r.status_code >= 500:
                errors.append(f"{method} {url} {value!r}: {r.status_code}")
    assert errors == []


def _templates_dir():
    from domownik.web import HERE

    return HERE / "templates"


def test_bad_input_is_a_readable_400(auth_client):
    r = auth_client.post("/zakupy", data={"name": "Chleb", "item_id": "abc"}, headers=HX)
    assert r.status_code == 400 and "Nieprawidłowe dane" in r.text and r.headers["hx-reswap"] == "none"
    r = auth_client.post("/punkty/przyznaj", data={"user_id": "9" * 30, "points": "1"})
    assert r.status_code == 400 and "<h2>Błąd 400</h2>" in r.text
    r = auth_client.post("/push/subscribe", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert auth_client.post("/push/subscribe", json=["a", "list"]).status_code == 400


def test_categories_page_with_an_ended_income(auth_client, db):
    """Regression: an income with an end date crashed /budzet/kategorie (date < function)."""
    db.add(RecurringIncome(name="Najem", amount_gr=1, starts_on=today().replace(day=1), ends_on=today()))
    db.commit()
    assert auth_client.get("/budzet/kategorie").status_code == 200


# ---------------------------------------------------------------- someone else's private task, down to its parts


@pytest.fixture
def secret(db, other):
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.flush()
    t = Task(
        title="Prezent-niespodzianka",
        item_id=house.id,
        category="occasion",
        recurrence={"mode": "once"},
        next_due_date=today() + timedelta(days=3),
        private_to_id=other.id,
    )
    db.add(t)
    db.flush()
    step = TaskStep(task_id=t.id, title="Zamówić")
    comp = Completion(task_id=t.id, done_on=today(), due_date=today())
    db.add_all([step, comp, Comment(task_id=t.id, user_id=other.id, body="Kupione")])
    db.flush()
    (task_file,) = documents.store_files(db, house, [("paragon-prezent.jpg", io.BytesIO(_jpeg()))], user=other)
    task_file.task_id = t.id
    (comp_file,) = documents.store_files(db, house, [("faktura-prezent.jpg", io.BytesIO(_jpeg()))], user=other)
    comp_file.completion_id = comp.id
    db.commit()
    return {"house": house, "task": t, "step": step, "comp": comp, "files": [task_file, comp_file]}


def test_private_task_parts_are_hidden(auth_client, db, secret):
    for att in secret["files"]:
        for suffix in ("", "/thumb", "/view"):
            assert auth_client.get(f"/files/{att.id}{suffix}").status_code == 404
        assert auth_client.post(f"/files/{att.id}/delete").status_code == 404
    page = auth_client.get(f"/items/{secret['house'].id}").text
    assert "prezent" not in page.lower()

    step_id, comp_id = secret["step"].id, secret["comp"].id
    assert auth_client.post(f"/kroki/{step_id}/toggle", headers=HX).status_code == 404
    assert auth_client.post(f"/kroki/{step_id}/delete", headers=HX).status_code == 404
    assert auth_client.post(f"/completions/{comp_id}/undo").status_code == 404
    db.expire_all()
    step = db.scalar(select(TaskStep).where(TaskStep.id == step_id))
    assert step is not None and not step.done
    assert db.scalar(select(Completion).where(Completion.id == comp_id)) is not None
    assert db.scalar(select(Attachment).where(Attachment.task_id.is_not(None))) is not None


def test_owner_still_reaches_them(client, db, secret):
    client.post(
        "/login",
        data={"email": "ola@example.com", "password": "tajnehaslo2", "csrf": csrf_of(client.get("/login").text)},
    )
    client.headers["X-CSRF-Token"] = csrf_of(client.get("/").text)
    for att in secret["files"]:
        assert client.get(f"/files/{att.id}").status_code == 200
    assert "paragon-prezent.jpg" in client.get(f"/items/{secret['house'].id}").text
    assert client.post(f"/kroki/{secret['step'].id}/toggle", headers=HX).status_code == 200


@pytest.mark.parametrize("param", ["task_id", "note_id", "todo_id", "cal_id"])
def test_no_route_touches_someone_elses_private_row(auth_client, db, other, param):
    """Every route that takes an id of a private-capable thing answers 404 for someone else's private one and
    leaves it as it was."""
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.flush()
    row = {
        "task_id": lambda: Task(
            title="Tajne",
            item_id=house.id,
            category="occasion",
            recurrence={"mode": "once"},
            next_due_date=today() + timedelta(days=2),
            private_to_id=other.id,
        ),
        "note_id": lambda: BoardNote(body="Tajne", private_to_id=other.id, created_by_id=other.id),
        "todo_id": lambda: Todo(title="Tajne", item_id=house.id, private_to_id=other.id),
        "cal_id": lambda: ExternalCalendar(
            name="Tajne", url="https://example.com/t.ics", user_id=other.id, private_to_id=other.id
        ),
    }[param]()
    db.add(row)
    db.commit()
    model, rid = type(row), row.id

    def snapshot():
        db.expire_all()
        r = db.scalar(select(model).where(model.id == rid).execution_options(all_private=True))
        return None if r is None else {c.key: getattr(r, c.key) for c in model.__table__.columns}

    before = snapshot()
    routes = [(m, p) for m, p, _ in _routes(auth_client.app) if "{" + param + "}" in p]
    assert routes
    data = {"title": "Zmienione", "body": "Zmienione", "name": "Zmienione", "to": today().isoformat(), "pinned": "1"}
    for method, path in routes:
        url = path.replace("{" + param + "}", str(rid))
        url = _url(url)  # other ids in the path (a photo, a step)
        for headers in ({}, HX):
            r = auth_client.request(method, url, data=data if method != "GET" else None, headers=headers)
            assert r.status_code == 404, f"{method} {url}: {r.status_code}"
            assert "Tajne" not in r.text
    assert snapshot() == before


# ---------------------------------------------------------------- login


def _login(client, email: str, password: str):
    return client.post(
        "/login", data={"email": email, "password": password, "csrf": csrf_of(client.get("/login").text)}
    )


def test_login_limit_counts_every_spelling_of_the_email(client, user):
    for email in ("jan@example.com", " jan@example.com", "JAN@example.com ", "Jan@Example.com", "\tjan@example.com"):
        assert _login(client, email, "zgaduje").status_code == 401
    r = _login(client, "jan@example.com", "tajnehaslo1")  # even the right password waits now
    assert r.status_code == 429 and "Za dużo prób" in r.text


def test_login_limit_across_many_emails(client, user):
    for i in range(security.MAX_FAILS_ALL):
        _login(client, f"ktos{i}@example.com", "zgaduje")
    assert _login(client, "jan@example.com", "tajnehaslo1").status_code == 429


def test_unknown_email_costs_a_password_check(client, user, monkeypatch):
    """No timing hint which addresses have an account: argon2 runs for unknown e-mails too."""
    calls = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password", lambda h, p: calls.append(h) or real(h, p))
    import domownik.routers.auth as auth

    monkeypatch.setattr(auth, "verify_password", security.verify_password)
    _login(client, "nikt@example.com", "zgaduje")
    _login(client, "jan@example.com", "zgaduje")
    assert len(calls) == 2


def test_session_is_new_after_login_and_gone_after_logout(client, user):
    before = csrf_of(client.get("/login").text)
    r = _login(client, "jan@example.com", "tajnehaslo1")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    after = csrf_of(client.get("/").text)
    assert after != before  # the pre-login session (and anything planted in it) is dropped
    client.post("/logout", data={"csrf": after})
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_secure_cookie_and_hsts_over_https(engine, user, monkeypatch):
    monkeypatch.setattr(get_settings(), "https_only", True)
    with TestClient(create_app(), base_url="https://testserver") as c:
        security._FAILS.clear()
        r = _login(c, "jan@example.com", "tajnehaslo1")
        assert "secure" in r.headers["set-cookie"].lower()
        assert r.headers["strict-transport-security"].startswith("max-age=")


def test_deactivated_account_is_logged_out(auth_client, db, user):
    user.is_active = False
    db.commit()
    assert auth_client.get("/", follow_redirects=False).headers["location"] == "/login"
    user.is_active = True
    db.commit()
    assert auth_client.get("/", follow_redirects=False).headers["location"] == "/login"  # stays logged out


def test_nothing_big_is_read_before_login(client):
    token = csrf_of(client.get("/login").text)
    r = client.post(
        "/login",
        data={"email": "a@example.com", "password": "x", "csrf": token},
        files={"f": ("big.bin", b"0" * 20000, "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.post("/login", data={"email": "a@example.com", "password": "x" * 20000, "csrf": token})
    assert r.status_code == 200 and "Nieprawidłowy" not in r.text  # back on the form, the password never checked


# ---------------------------------------------------------------- responses


def test_security_headers(auth_client, db, user):
    for r in (auth_client.get("/"), auth_client.get("/login"), auth_client.get("/static/app.css")):
        assert r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.flush()
    (att,) = documents.store_files(db, house, [("a.jpg", io.BytesIO(_jpeg()))], user=user)
    db.commit()
    photo = auth_client.get(f"/files/{att.id}")
    assert "sandbox" in photo.headers["content-security-policy"]  # the route's own, stricter policy stays


def test_error_messages_are_escaped(client):
    payload = "<img src=x onerror=alert(1)>"

    @client.app.get("/_boom")
    def boom():
        raise HTTPException(400, f"Invalid isoformat string: '{payload}'")

    r = client.get("/_boom", headers=HX)
    assert payload not in r.text and "&lt;img" in r.text
    assert payload not in client.get("/_boom").text


def test_redirect_targets_stay_on_the_site(auth_client):
    for nxt in ("//evil.example", "/\\evil.example", "https://evil.example", "/%09/evil.example", "javascript:x"):
        r = auth_client.post("/zakupy", params={"next": nxt}, data={"name": "Mleko"}, follow_redirects=False)
        loc = r.headers["location"]
        # where a browser really goes (it reads "\\" as "/"): still this site
        assert "\\" not in loc and urlsplit(urljoin("https://dom.example/zakupy", loc)).netloc == "dom.example", loc


def test_push_only_to_browser_push_services(auth_client, db):
    keys = {"p256dh": "BPk", "auth": "xyz"}
    for bad in (
        "http://fcm.googleapis.com/fcm/send/a",
        "https://127.0.0.1/x",
        "https://192.168.1.1/admin",
        "https://localhost/x",
        "https://router.lan/x",
        "https://fcm.googleapis.com.evil.example/x",
        "https://evilfcm.googleapis.com.example/x",
        "https://fcm.googleapis.com:8443/x",
        "https://fcm.googleapis.com/" + "a" * 1000,
    ):
        assert auth_client.post("/push/subscribe", json={"endpoint": bad, "keys": keys}).status_code == 400, bad
    for good in (
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://web.push.apple.com/abc",
    ):
        assert auth_client.post("/push/subscribe", json={"endpoint": good, "keys": keys}).status_code == 200


def test_push_never_sent_to_an_old_odd_endpoint(user):
    from domownik.models import PushSubscription

    sub = PushSubscription(user_id=user.id, endpoint="https://10.0.0.5/hook", p256dh="k", auth="a")
    assert webpush.deliver(sub, "{}") is False  # dropped as gone, no request made


def test_done_link_answers_plain_text(client, db, user):
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.flush()
    t = Task(
        title="<img src=x onerror=alert(1)>",
        item_id=house.id,
        category="service",
        recurrence={"mode": "once"},
        next_due_date=today(),
    )
    db.add(t)
    db.commit()
    token = notify.done_token(t, user)
    r = client.post(f"/n/done/{token[:-2]}xx")
    assert r.status_code == 403
    r = client.post(f"/n/done/{token}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    again = client.post(f"/n/done/{token}")  # replay: nothing more happens
    assert again.text.startswith("Już odhaczone") and again.headers["content-type"].startswith("text/plain")
    db.expire_all()
    assert db.scalar(select(Completion).where(Completion.task_id == t.id)) is not None
    assert len(list(db.scalars(select(Completion)))) == 1
