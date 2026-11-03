"""/tasks/{id}: the window over a list for htmx, a whole page for a refreshed or opened link."""

from datetime import timedelta

from domownik.config import today
from domownik.models import Item, Task

HX = {"HX-Request": "true"}


def _task(db, *, recurring=True, due_in=3):
    item = Item(name="Auto 1", kind="car")
    db.add(item)
    db.flush()
    rule = {"mode": "interval", "every": 1, "unit": "year", "anchor": "due"} if recurring else {"mode": "once"}
    task = Task(
        item_id=item.id,
        title="Przegląd",
        category="inspection",
        recurrence=rule,
        next_due_date=today() + timedelta(days=due_in),
        direction="expense",
    )
    db.add(task)
    db.commit()
    return task


def test_htmx_gets_the_window_fragment(auth_client, db):
    task = _task(db)
    r = auth_client.get(f"/tasks/{task.id}", headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text and "Przegląd" in r.text
    assert "data-close" in r.text  # the window's ✕


def test_plain_request_gets_a_full_page(auth_client, db):
    task = _task(db)
    r = auth_client.get(f"/tasks/{task.id}")
    assert r.status_code == 200
    assert r.text.lstrip().startswith("<!doctype html>")
    assert 'id="sidebar"' in r.text  # layout with the menu
    assert "<title>Przegląd · Domownik</title>" in r.text
    assert f'href="/items/{task.item_id}"' in r.text and "← Pulpit" in r.text  # way back
    assert "data-close" not in r.text.split('<dialog id="modal"')[0]  # no ✕ outside the (empty) window
    assert f'hx-post="/tasks/{task.id}/done"' in r.text  # actions still there


def test_boosted_navigation_gets_a_full_page(auth_client, db):
    task = _task(db)
    r = auth_client.get(f"/tasks/{task.id}", headers={**HX, "HX-Boosted": "true"})
    assert 'id="sidebar"' in r.text


def test_missing_task_is_404(auth_client):
    assert auth_client.get("/tasks/999").status_code == 404


def test_list_rows_link_to_the_task_page(auth_client, db):
    task = _task(db)
    page = auth_client.get(f"/items/{task.item_id}").text
    assert f'href="/tasks/{task.id}" hx-get="/tasks/{task.id}"' in page


def test_actions_from_the_window_reload_the_list(auth_client, db):
    task = _task(db)
    r = auth_client.post(f"/tasks/{task.id}/done", data={}, headers={**HX, "HX-Current-URL": "http://testserver/"})
    assert r.headers.get("HX-Refresh") == "true" and "HX-Redirect" not in r.headers


def test_done_and_skip_on_the_page_go_back_to_the_zone(auth_client, db):
    task = _task(db)
    here = {**HX, "HX-Current-URL": f"http://testserver/tasks/{task.id}"}
    r = auth_client.post(f"/tasks/{task.id}/done", data={}, headers=here)
    assert r.headers.get("HX-Redirect") == f"/items/{task.item_id}"
    r = auth_client.post(f"/tasks/{task.id}/skip", headers=here)
    assert r.headers.get("HX-Redirect") == f"/items/{task.item_id}"


def test_postpone_on_the_page_stays_there(auth_client, db):
    task = _task(db)
    here = {**HX, "HX-Current-URL": f"http://testserver/tasks/{task.id}"}
    to = (today() + timedelta(days=10)).isoformat()
    r = auth_client.post(f"/tasks/{task.id}/postpone", data={"to": to}, headers=here)
    assert r.headers.get("HX-Refresh") == "true"


def test_finished_one_off_on_the_page_leaves_it(auth_client, db):
    task = _task(db, recurring=False)
    here = {**HX, "HX-Current-URL": f"http://testserver/tasks/{task.id}?x=1"}
    r = auth_client.post(f"/tasks/{task.id}/done", data={}, headers=here)
    assert r.headers.get("HX-Redirect") == f"/items/{task.item_id}"
