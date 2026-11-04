"""📋 Zadania: every dated task in one list, by due bucket, with a category filter."""

from __future__ import annotations

from datetime import timedelta

from domownik.config import today
from domownik.models import Item, Task, Todo


def _setup(db):
    home = Item(name="Dom", kind="zone")
    db.add(home)
    db.flush()
    t = today()
    db.add_all(
        [
            Task(
                item_id=home.id,
                title="Czynsz",
                category="payment",
                next_due_date=t + timedelta(days=3),
                recurrence={"mode": "once"},
                lead_days=7,
                direction="expense",
                amount_gr=150000,
            ),
            Task(
                item_id=home.id,
                title="Przegląd kotła",
                category="inspection",
                next_due_date=t - timedelta(days=2),
                recurrence={"mode": "once"},
                lead_days=7,
                direction="none",
            ),
            Task(
                item_id=home.id,
                title="Wymiana filtra",
                category="service",
                next_due_date=t + timedelta(days=200),
                recurrence={"mode": "once"},
                lead_days=7,
                direction="none",
            ),
        ]
    )
    db.add(Todo(title="Sprawa bez terminu", item_id=home.id))
    db.commit()


def test_tasks_page_lists_all_dated_tasks(auth_client, db):
    _setup(db)
    html = auth_client.get("/zadania").text
    for title in ("Czynsz", "Przegląd kotła", "Wymiana filtra"):
        assert title in html
    assert "Sprawa bez terminu" not in html  # undated ones live in ☐ Do zrobienia
    assert "Zaległe" in html and "Później" in html
    assert 'href="/zadania"' in html  # in the menu


def test_tasks_page_category_filter(auth_client, db):
    _setup(db)
    html = auth_client.get("/zadania?kat=payment").text
    assert "Czynsz" in html and "Przegląd kotła" not in html
    # an unknown category shows everything
    html = auth_client.get("/zadania?kat=nonsense").text
    assert "Czynsz" in html and "Przegląd kotła" in html
