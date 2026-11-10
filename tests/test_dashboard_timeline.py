"""Dashboard readability: bundled timeline points, section order, empty states, long "Później"."""

from __future__ import annotations

from datetime import date, timedelta

from domownik import services
from domownik.config import today
from domownik.models import Item, Task

MONTHLY = {"mode": "monthly", "day": 10}


def _home(db) -> Item:
    item = Item(name="Dom", kind="house")
    db.add(item)
    db.commit()
    return item


def _task(db, item, title, due, *, category="payment", recurrence=None, lead_days=7) -> Task:
    task = Task(
        item_id=item.id,
        title=title,
        category=category,
        recurrence=recurrence or {"mode": "once"},
        next_due_date=due,
        amount_gr=10000,
        direction="expense",
        lead_days=lead_days,
    )
    db.add(task)
    db.commit()
    return task


def _p(title, d, *, status="later", kind="due", category="payment", icon="💳", group=1):
    return {
        "task_id": hash(title) % 1000,
        "title": title,
        "zone": "Dom",
        "icon": icon,
        "category": category,
        "amount": 100,
        "direction": "expense",
        "group": group,
        "date": d,
        "kind": kind,
        "status": status,
    }


def test_plural_pl():
    assert [services.plural_pl(n, "zadanie", "zadania", "zadań") for n in (1, 2, 4, 5, 12, 22, 25)] == [
        "zadanie",
        "zadania",
        "zadania",
        "zadań",
        "zadań",
        "zadania",
        "zadań",
    ]


def test_bundle_points_same_day_payments_become_one_labelled_point():
    pts = [_p(f"Opłata {i}", "2026-11-10") for i in range(5)] + [_p("Samotna", "2026-11-12")]
    out = services.bundle_points(pts, "day")
    bundles = [p for p in out if p["kind"] == "bundle"]
    assert len(bundles) == 1
    b = bundles[0]
    assert b["count"] == 5 and b["label"] == "Opłaty" and b["category"] == "payment"
    assert b["summary"] == "5 zadań" and b["date"] == "2026-11-10" and b["icons"] == ["💳"]
    assert len(b["tasks"]) == 5 and all("title" in t and "task_id" in t for t in b["tasks"])
    # a lone point stays itself
    assert any(p["kind"] == "due" and p["title"] == "Samotna" for p in out)


def test_bundle_points_never_hide_urgent_or_forecast():
    pts = [_p(f"Opłata {i}", "2026-11-05") for i in range(3)]
    pts += [
        _p("Przegląd", "2026-11-05", status="soon", category="inspection", icon="🔍"),
        _p("Zaległe", "2026-11-05", status="overdue"),
        _p("Dziś", "2026-11-05", status="today"),
        _p("Prognoza", "2026-11-05", kind="forecast", status="later"),
    ]
    out = services.bundle_points(pts, "month")
    singles = {p["title"] for p in out if p["kind"] != "bundle"}
    assert singles == {"Przegląd", "Zaległe", "Dziś", "Prognoza"}
    (b,) = [p for p in out if p["kind"] == "bundle"]
    assert b["count"] == 3 and b["start"] == "2026-11-01" and b["end"] == "2026-11-30"


def test_bundle_points_mixed_week_and_done_kept_apart():
    pts = [_p("A", "2026-11-09"), _p("B", "2026-11-10"), _p("C", "2026-11-10")]
    pts += [_p("Serwis", "2026-11-11", category="service", icon="🛠️")]
    pts += [
        _p("Zrobione 1", "2026-11-09", kind="done", status="done"),
        _p("Zrobione 2", "2026-11-10", kind="done", status="done"),
    ]
    out = services.bundle_points(pts, "week")
    due = [p for p in out if p["kind"] == "bundle" and p["of"] == "due"]
    done = [p for p in out if p["kind"] == "bundle" and p["of"] == "done"]
    assert len(due) == 1 and len(done) == 1
    b = due[0]
    # Monday-based week, placed on its busiest day, says what it contains
    assert (b["start"], b["end"], b["date"]) == ("2026-11-09", "2026-11-15", "2026-11-10")
    assert b["category"] is None and b["label"] == "4 zadania"
    assert b["summary"] == "4 zadania: Opłaty 3, Serwisy 1" and b["icons"][0] == "💳"
    assert done[0]["status"] == "done" and done[0]["count"] == 2


def test_bundle_points_per_lane():
    pts = [_p("A", "2026-11-10", group=1), _p("B", "2026-11-10", group=1), _p("C", "2026-11-10", group=2)]
    assert len([p for p in services.bundle_points(pts, "day") if p["kind"] == "bundle"]) == 1
    lanes = services.bundle_points(pts, "day", by_group=True)
    assert [p["kind"] for p in lanes].count("bundle") == 1 and len(lanes) == 2


def test_timeline_api_bins(auth_client, db):
    item = _home(db)
    t = today()
    pay_day = t + timedelta(days=20)
    for i in range(6):
        _task(db, item, f"Opłata {i}", pay_day)
    _task(db, item, "Przegląd", t + timedelta(days=3), category="inspection", lead_days=14)
    params = {"start": t.isoformat(), "end": (t + timedelta(days=60)).isoformat()}

    raw = auth_client.get("/api/timeline", params=params).json()["points"]
    assert len(raw) == 7  # without bin: one point per occurrence, as before

    pts = auth_client.get("/api/timeline", params=params | {"bin": "day"}).json()["points"]
    assert len(pts) == 2
    (bundle,) = [p for p in pts if p["kind"] == "bundle"]
    assert bundle["count"] == 6 and bundle["date"] == pay_day.isoformat() and bundle["label"] == "Opłaty"
    (soon,) = [p for p in pts if p["kind"] == "due"]
    assert soon["title"] == "Przegląd" and soon["status"] == "soon"

    assert auth_client.get("/api/timeline", params=params | {"bin": "year"}).status_code == 400


def test_dashboard_order_empty_week_and_long_later(auth_client, db):
    item = _home(db)
    t = today()
    _task(db, item, "Za dwa tygodnie", t + timedelta(days=14), category="service")
    for i in range(12):
        _task(db, item, f"Daleko {i:02d}", t + timedelta(days=60 + i), category="service")
    html = auth_client.get("/").text
    # timeline on top, then the upcoming dates, tiles only below them
    main = html[html.index("<main") :]
    assert main.index('id="timeline"') < main.index("Najbliższe terminy")
    assert main.index("Najbliższe terminy") < main.index(">Domownicy</h2>") < main.index(">Strefy</h2>")
    assert "nic w tym tygodniu ✓" in html
    assert "Pokaż wszystkie (12)" in html
    assert html.index("Daleko 04") < html.index("Pokaż wszystkie (12)") < html.index("Daleko 05")
    assert "Daleko 11" in html  # nothing silently cut off


def test_dashboard_short_later_not_collapsed(auth_client, db):
    item = _home(db)
    t = today()
    for i in range(3):
        _task(db, item, f"Daleko {i}", t + timedelta(days=60 + i), category="service")
    html = auth_client.get("/").text
    assert "Pokaż wszystkie" not in html and "Daleko 2" in html


def test_bin_range_month_end():
    assert services._bin_range(date(2026, 12, 31), "month") == (date(2026, 12, 1), date(2026, 12, 31))
    assert services._bin_range(date(2026, 2, 3), "month") == (date(2026, 2, 1), date(2026, 2, 28))
