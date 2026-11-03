"""Child's vaccination and check-up calendar from the birthday."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from domownik.domain import child_health
from domownik.models import Item, Task


def test_plan_dates_and_flags():
    born = date(2025, 1, 15)
    plan = child_health.plan_for(born, date(2026, 11, 3), {"bilans 1-latka"})
    by = {o.visit.key: o for o in plan}
    assert by["v2m"].due == date(2025, 3, 15) and by["v2m"].past
    assert by["b2y"].due == date(2027, 1, 15) and not by["b2y"].past
    assert by["b12m"].planned
    assert by["v6y"].due == date(2031, 1, 15)
    assert len({v.key for v in child_health.PLAN}) == len(child_health.PLAN)


def test_page_and_adding(auth_client, db):
    kid = Item(name="Staś", kind="child", attributes={"birthday": "2025-01-15"})
    nobday = Item(name="Zosia", kind="child")
    db.add_all([kid, nobday])
    db.commit()
    page = auth_client.get(f"/items/{kid.id}").text
    assert "Szczepienia i bilanse" in page and "Bilans 2-latka" in page
    assert "datę urodzenia" in auth_client.get(f"/items/{nobday.id}").text

    auth_client.post(f"/items/{kid.id}/szczepienia", data={"v": ["b2y", "v6y", "nope"]})
    auth_client.post(f"/items/{kid.id}/szczepienia", data={"v": ["b2y"]})  # twice → once
    tasks = db.scalars(select(Task).where(Task.item_id == kid.id).order_by(Task.next_due_date)).all()
    assert [(t.title, t.next_due_date) for t in tasks] == [
        ("Bilans 2-latka", date(2027, 1, 15)),
        ("Szczepienie – 6 lat", date(2031, 1, 15)),
    ]
    assert tasks[0].category == "health" and tasks[0].direction == "none" and "przychodnia" in tasks[0].notes
    assert "✓ w zadaniach" in auth_client.get(f"/items/{kid.id}").text
    assert auth_client.post(f"/items/{nobday.id}/szczepienia", data={"v": ["b2y"]}).status_code == 400
