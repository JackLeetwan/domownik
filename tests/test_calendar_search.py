"""Month calendar (/kalendarz) and the search box (/szukaj)."""

from __future__ import annotations

from datetime import date, timedelta

from domownik import services
from domownik.config import today
from domownik.domain.text import fold, snippet, terms
from domownik.domain.tree import Tree
from domownik.models import BoardNote, Completion, Issue, Item, JournalEntry, Task, Todo, User
from domownik.search import search
from domownik.security import hash_password


def _zone(db, name="Dom", kind="house", **kw) -> Item:
    item = Item(name=name, kind=kind, **kw)
    db.add(item)
    db.commit()
    return item


def _task(db, item, title, due, *, category="payment", recurrence=None, assignee=None, notes=None) -> Task:
    t = Task(
        item_id=item.id,
        title=title,
        category=category,
        recurrence=recurrence or {"mode": "once"},
        next_due_date=due,
        amount_gr=10000,
        direction="expense",
        lead_days=7,
        assignee_id=assignee.id if assignee else None,
        notes=notes,
    )
    db.add(t)
    db.commit()
    return t


def _next_month(d: date) -> date:
    return (d.replace(day=1) + timedelta(days=32)).replace(day=1)


# ---------------------------------------------------------------- calendar


def test_month_grid_starts_monday_and_covers_month():
    start, end = services.month_grid(2026, 11)
    assert start == date(2026, 10, 26) and start.weekday() == 0
    assert end == date(2026, 12, 6) and end.weekday() == 6


def test_month_payments_on_one_day_become_one_chip(db):
    home = _zone(db)
    m = _next_month(today())
    tenth = m.replace(day=10)
    for i in range(17):
        _task(db, home, f"Rachunek {i}", tenth, recurrence={"mode": "monthly", "day": 10})
    _task(db, home, "Przegląd kotła", tenth, category="inspection")
    cal = services.calendar_month(db, Tree.load(db), today(), m.year, m.month)
    assert cal.title.startswith(
        (
            "styczeń",
            "luty",
            "marzec",
            "kwiecień",
            "maj",
            "czerwiec",
            "lipiec",
            "sierpień",
            "wrzesień",
            "październik",
            "listopad",
            "grudzień",
        )
    )
    day = next(d for d in cal.days() if d.date == tenth)
    bundles = [e for e in day.entries if e["kind"] == "bundle"]
    assert len(bundles) == 1 and bundles[0]["count"] == 17 and bundles[0]["label"] == "Opłaty"
    assert [e["title"] for e in day.entries if e["kind"] != "bundle"] == ["Przegląd kotła"]
    assert day.count == 18
    # monthly bills show up in the following month too, but the one-off does not
    nxt = _next_month(m)
    cal2 = services.calendar_month(db, Tree.load(db), today(), nxt.year, nxt.month)
    d2 = next(d for d in cal2.days() if d.date == nxt.replace(day=10))
    assert d2.count == 17


def test_series_numbers_and_end(db):
    home = _zone(db)
    m = _next_month(today())
    first = m.replace(day=5)
    _task(db, home, "Rata za lodówkę", first, recurrence={"mode": "monthly", "day": 5, "count": 2})
    tree = Tree.load(db)
    cal = services.calendar_month(db, tree, today(), m.year, m.month)
    e = next(d for d in cal.days() if d.date == first).entries
    assert e[0]["title"] == "Rata za lodówkę (rata 1/2)"
    n1 = _next_month(m)
    cal = services.calendar_month(db, tree, today(), n1.year, n1.month)
    assert next(d for d in cal.days() if d.date == n1.replace(day=5)).entries[0]["title"].endswith("(rata 2/2)")
    n2 = _next_month(n1)
    cal = services.calendar_month(db, tree, today(), n2.year, n2.month)
    assert not any(d.entries for d in cal.days() if d.in_month)  # the series has ended


def test_done_in_past_and_default_day(db, user):
    home = _zone(db)
    t = _task(db, home, "Wymiana filtra", today() + timedelta(days=40), category="service")
    db.add(Completion(task_id=t.id, done_on=today(), due_date=today()))
    db.commit()
    cal = services.calendar_month(db, Tree.load(db), today(), today().year, today().month)
    tday = next(d for d in cal.days() if d.date == today())
    assert tday.is_today and tday.entries[0]["status"] == "done"
    assert cal.default_day(today()) == today()


def test_filter_mine(db, user):
    other = User(email="ala@example.com", name="Ala", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.commit()
    home = _zone(db)
    d = today() + timedelta(days=3)
    _task(db, home, "Moje", d, category="other", assignee=user)
    _task(db, home, "Wspólne", d, category="service")
    _task(db, home, "Ali", d, category="health", assignee=other)
    pts = services.calendar_day(db, Tree.load(db), today(), d, user_id=user.id)
    assert sorted(p["title"] for p in pts) == ["Moje", "Wspólne"]
    assert len(services.calendar_day(db, Tree.load(db), today(), d)) == 3


def test_calendar_pages(auth_client, db, user):
    home = _zone(db)
    m = _next_month(today())
    tenth = m.replace(day=10)
    for i in range(3):
        _task(db, home, f"Rachunek {i}", tenth)
    r = auth_client.get(f"/kalendarz?m={m:%Y-%m}")
    assert r.status_code == 200
    assert services.MONTH_NAMES[m.month] in r.text and "Opłaty" in r.text
    # nothing earlier that month → the busy 10th is the selected day
    assert f'data-sel="{tenth.isoformat()}"' in r.text
    r = auth_client.get(f"/kalendarz/dzien?d={tenth}")
    assert r.status_code == 200 and "Rachunek 2" in r.text and 'id="cal-day"' in r.text
    r = auth_client.get(f"/kalendarz/grupa?d={tenth}&of=due&cat=payment")
    assert r.status_code == 200 and r.text.count('hx-get="/tasks/') == 3
    assert auth_client.get(f"/kalendarz/grupa?d={tenth}&of=due&cat=tax").status_code == 404
    assert auth_client.get("/kalendarz?m=2026-13").status_code == 400
    # the way in: sidebar entry and a link in the dashboard timeline card
    dash = auth_client.get("/").text
    assert 'href="/kalendarz"' in dash and "Kalendarz →" in dash


def test_calendar_requires_login(client):
    r = client.get("/kalendarz", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# ---------------------------------------------------------------- search


def test_fold_and_terms():
    assert fold("Żółtko ŁÓDŹ") == "zoltko lodz"
    assert terms("  Olej  SILNIKOWY ") == ["olej", "silnikowy"]
    html = str(snippet("Kupić żółtko i mąkę", terms("zoltko")))
    assert html == "Kupić <mark>żółtko</mark> i mąkę"
    assert "&lt;b&gt;" in str(snippet("<b>olej</b>", ["olej"]))


def test_search_polish_letters_and_case(db):
    home = _zone(db)
    _task(db, home, "Wymiana oleju", today(), category="service", notes="Olej 5W30")
    db.add(Todo(title="Kupić żółtko", item_id=None))
    db.commit()
    res = search(db, "olej")
    assert [str(h.title) for h in res.groups["task"]] == ["Wymiana <mark>olej</mark>u"]
    assert "<mark>Olej</mark> 5W30" in str(res.groups["task"][0].detail)
    assert "todo" in search(db, "ZOLTKO").groups


def test_search_all_words_must_match(db):
    home = _zone(db)
    _task(db, home, "Ubezpieczenie domu", today(), category="insurance")
    _task(db, home, "Ubezpieczenie auta", today(), category="insurance")
    res = search(db, "ubezpieczenie domu")
    assert [str(h.title) for h in res.groups["task"]] == ["<mark>Ubezpieczenie</mark> <mark>domu</mark>"]
    assert len(search(db, "UBEZPIECZENIE").groups["task"]) == 2
    assert search(db, "ubezpieczenie roweru").groups == {}


def test_search_item_attributes_and_notes(db):
    cars = _zone(db, "Samochody", "group")
    car = _zone(
        db,
        "Auto 1",
        "car",
        parent_id=cars.id,
        attributes={"vin": "WVWZZZ1KZ6W123456", "plate": "XY 12345", "_suggest": ["oc"]},
        notes="Polisa nr ABC-778",
    )
    res = search(db, "1kz6w")
    hit = res.groups["item"][0]
    assert hit.url == f"/items/{car.id}" and hit.path == "Samochody"
    assert "<mark>1KZ6W</mark>" in str(hit.detail)
    assert search(db, "xy 123").groups["item"][0].url == f"/items/{car.id}"
    assert "abc-778" in str(search(db, "abc-778").groups["item"][0].detail).lower()
    assert search(db, "suggest").groups == {}  # internal keys are not searched


def test_search_other_kinds(db, user):
    home = _zone(db)
    t = _task(db, home, "Serwis pieca", today(), category="service")
    db.add_all(
        [
            Issue(item_id=home.id, title="Cieknie kran", started_on=today()),
            JournalEntry(item_id=home.id, noted_on=today(), body="Hydraulik wymienił uszczelkę"),
            BoardNote(body="Wi-Fi gości: hasło w szufladzie"),
            Completion(task_id=t.id, done_on=today(), vendor="Firma Kowalski", notes="wymiana dyszy"),
        ]
    )
    db.commit()
    assert "issue" in search(db, "kran").groups
    assert "journal" in search(db, "uszczelke").groups
    assert "note" in search(db, "wifi").groups or "note" in search(db, "wi-fi").groups
    res = search(db, "kowalski")
    assert res.groups["done"][0].task_id == t.id
    # the task title alone gives a task hit, not a history hit
    assert "done" not in search(db, "serwis pieca").groups


def test_search_skips_archived(db):
    _zone(db, "Stary dom", archived=True)
    assert search(db, "stary").groups == {}


def test_search_pages(auth_client, db):
    home = _zone(db)
    _task(db, home, "Wymiana oleju", today(), category="service")
    r = auth_client.get("/szukaj?q=OLEJ")
    assert r.status_code == 200 and "<mark>olej</mark>" in r.text and "Zadania" in r.text
    r = auth_client.get("/szukaj/na-zywo?q=olej")
    assert r.status_code == 200 and "Wszystkie wyniki (1)" in r.text and "<html" not in r.text
    r = auth_client.get("/szukaj?q=rower")
    assert "Nic nie znaleziono dla „rower”" in r.text
    assert auth_client.get("/szukaj/na-zywo?q=o").text.strip() == ""
    assert 'id="side-search"' in auth_client.get("/").text


def test_search_requires_login(client):
    for url in ("/szukaj?q=olej", "/szukaj/na-zywo?q=olej"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_only_the_current_date_is_draggable(auth_client, db):
    """Drag & drop in the calendar posts to "Przełóż": only a task's current date carries the handle."""
    item = _zone(db)
    due = today() + timedelta(days=3)
    _task(db, item, "Czynsz", due, recurrence={"mode": "monthly", "day": due.day})
    page = auth_client.get(f"/kalendarz/tydzien?d={due.isoformat()}").text
    assert page.count('data-drag-task="') == 1 and f'data-drag-from="{due.isoformat()}"' in page
    from dateutil.relativedelta import relativedelta

    nxt = auth_client.get(f"/kalendarz/tydzien?d={(due + relativedelta(months=1)).isoformat()}").text
    assert "Czynsz" in nxt and "data-drag-task" not in nxt  # a later occurrence is not moved by dragging


def test_delete_from_the_task_window(auth_client, db):
    """The window opened from the calendar has "🗑 Usuń"; deleting there stays on the calendar."""
    item = _zone(db)
    t = _task(db, item, "Spotkanie", today() + timedelta(days=5), category="event")
    tid = t.id
    window = auth_client.get(f"/tasks/{t.id}", headers={"HX-Request": "true"}).text
    assert "🗑 Usuń" in window and f'hx-post="/tasks/{t.id}/delete"' in window
    r = auth_client.post(
        f"/tasks/{t.id}/delete", headers={"HX-Request": "true", "HX-Current-URL": "http://testserver/kalendarz"}
    )
    assert r.headers.get("HX-Refresh") == "true"
    db.expire_all()
    assert db.get(Task, tid) is None
