"""A done point opens that completion, not a form for ticking off the next one; no second tick of the same day."""

from __future__ import annotations

from datetime import timedelta

import pytest

from domownik import services
from domownik.config import today
from domownik.models import Completion, Item, Task, Transaction

HX = {"HX-Request": "true"}
YEARLY = {"mode": "interval", "every": 1, "unit": "year", "anchor": "completion"}
MONTHLY = {"mode": "interval", "every": 1, "unit": "month", "anchor": "due"}


@pytest.fixture
def pump(db):
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.flush()
    t = Task(
        item_id=house.id,
        title="Serwis pompy",
        category="service",
        recurrence=YEARLY,
        next_due_date=today() - timedelta(days=50),
        amount_gr=45000,
        lead_days=30,
    )
    db.add(t)
    db.commit()
    comp = services.complete_task(
        db, t, user=None, done_on=today() - timedelta(days=50), amount_gr=45000, km=None, vendor=None, notes=None
    )
    return t, comp


def test_done_point_carries_its_completion(db, pump):
    from domownik.domain.tree import Tree

    _, comp = pump
    pts = services.timeline(db, Tree.load(db), today(), today() - timedelta(days=90), today())["points"]
    (done,) = [p for p in pts if p["kind"] == "done"]
    assert done["completion_id"] == comp.id


def test_window_on_a_done_point(auth_client, db, pump):
    t, comp = pump
    html = auth_client.get(f"/tasks/{t.id}?wykonanie={comp.id}", headers=HX).text
    assert 'class="comp-focus"' in html and "<strong>✓ Zrobione " in html and "450" in html
    assert 'class="done-early"' in html and "Odhacz następny termin" in html
    assert f"/completions/{comp.id}/undo" in html


def test_far_next_due_shows_last_and_hides_the_tick(auth_client, db, pump):
    t, _ = pump
    html = auth_client.get(f"/tasks/{t.id}", headers=HX).text
    assert "Ostatnio zrobione" in html and "Odhacz przed terminem" in html


def test_due_task_keeps_the_tick_up_front(auth_client, db):
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.flush()
    t = Task(item_id=house.id, title="Rachunek", category="payment", recurrence=MONTHLY, next_due_date=today())
    db.add(t)
    db.commit()
    html = auth_client.get(f"/tasks/{t.id}", headers=HX).text
    assert "done-early" not in html and "✓ Zrobione</button>" in html


def test_second_tick_of_the_same_day_is_refused(auth_client, db, pump):
    t, comp = pump
    r = auth_client.post(f"/tasks/{t.id}/done", data={"done_on": comp.done_on.isoformat(), "amount": "470"}, headers=HX)
    assert r.status_code == 422 and "Już odhaczone" in r.text
    assert db.query(Completion).count() == 1 and db.query(Transaction).count() == 1


def test_catching_up_two_overdue_months_the_same_day_still_works(auth_client, db):
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.flush()
    t = Task(
        item_id=house.id,
        title="Czynsz",
        category="payment",
        recurrence=MONTHLY,
        next_due_date=today() - timedelta(days=62),
    )
    db.add(t)
    db.commit()
    for _ in range(2):
        r = auth_client.post(f"/tasks/{t.id}/done", data={"done_on": today().isoformat()}, headers=HX)
        assert r.status_code == 200
    assert db.query(Completion).count() == 2


def test_a_foreign_completion_id_is_ignored(auth_client, db, pump):
    t, _ = pump
    html = auth_client.get(f"/tasks/{t.id}?wykonanie=9999", headers=HX).text
    assert "comp-focus" not in html and "Ostatnio zrobione" in html
