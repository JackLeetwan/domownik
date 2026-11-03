"""Checklist of a task: add, tick, delete, progress badge, fresh start for the next occurrence."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from domownik import services
from domownik.config import today
from domownik.models import Item, Task, TaskStep

HX = {"HX-Request": "true"}


def _task(db, **kw) -> Task:
    item = Item(name="Dom", kind="house")
    db.add(item)
    db.commit()
    t = Task(
        item_id=item.id,
        title="Odkamienianie",
        category="service",
        recurrence=kw.pop("recurrence", {"mode": "interval", "every": 1, "unit": "month", "anchor": "due"}),
        next_due_date=today() + timedelta(days=2),
        lead_days=7,
        direction="none",
        **kw,
    )
    db.add(t)
    db.commit()
    return t


def test_add_tick_delete_steps(auth_client, db):
    t = _task(db)
    modal = auth_client.get(f"/tasks/{t.id}", headers=HX).text
    assert f'id="steps-{t.id}"' in modal and "Dodaj krok" in modal

    r = auth_client.post(f"/tasks/{t.id}/steps", data={"title": "Zdjąć filtr"}, headers=HX)
    assert r.status_code == 200 and "Zdjąć filtr" in r.text and "0/1" in r.text
    auth_client.post(f"/tasks/{t.id}/steps", data={"title": "Przepłukać\nZałożyć filtr\n "}, headers=HX)
    steps = list(db.scalars(select(TaskStep).order_by(TaskStep.sort_order)))
    assert [s.title for s in steps] == ["Zdjąć filtr", "Przepłukać", "Założyć filtr"]

    r = auth_client.post(f"/kroki/{steps[0].id}/toggle", headers=HX)
    assert "1/3" in r.text and "step-done" in r.text
    r = auth_client.post(f"/kroki/{steps[1].id}/delete", headers=HX)
    assert "Przepłukać" not in r.text and "1/2" in r.text

    page = auth_client.get(f"/items/{t.item_id}").text
    assert "☑ 1/2" in page
    assert auth_client.post("/kroki/9999/toggle", headers=HX).status_code == 404


def test_steps_require_csrf(client, db, user):
    t = _task(db)
    assert client.post(f"/tasks/{t.id}/steps", data={"title": "x"}).status_code in (303, 401, 403)


def test_steps_reset_for_next_occurrence_but_not_for_one_off(db, user):
    t = _task(db)
    t.steps = [TaskStep(title="a", done=True, sort_order=1), TaskStep(title="b", done=True, sort_order=2)]
    db.commit()
    services.complete_task(db, t, user=user, done_on=today(), amount_gr=None, km=None, vendor=None, notes=None)
    assert [s.done for s in t.steps] == [False, False]

    once = _task(db, recurrence={"mode": "once"})
    once.steps = [TaskStep(title="a", done=True, sort_order=1)]
    db.commit()
    services.complete_task(db, once, user=user, done_on=today(), amount_gr=None, km=None, vendor=None, notes=None)
    assert once.active is False and once.steps[0].done is True  # history: what was done stays ticked


def test_deleting_task_deletes_steps(db):
    t = _task(db)
    t.steps = [TaskStep(title="a", sort_order=1)]
    db.commit()
    db.delete(t)
    db.commit()
    assert db.scalar(select(TaskStep)) is None
