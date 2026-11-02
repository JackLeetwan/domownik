from datetime import timedelta

from sqlalchemy import select

from domownik.config import today
from domownik.models import Issue, Item, JournalEntry, Task, Todo


def _car(auth_client, db):
    auth_client.post("/setup/zones")
    cars = db.scalar(select(Item).where(Item.name == "Samochody"))
    auth_client.post("/items/new/car", data={"parent_id": cars.id, "name": "Fiat"})
    return cars, db.scalar(select(Item).where(Item.name == "Fiat"))


def test_todo_add_toggle_and_dashboard(auth_client, db, user):
    cars, fiat = _car(auth_client, db)
    r = auth_client.post(
        f"/items/{fiat.id}/todos",
        data={"title": "Wyszlifować reflektory", "notes": "papier 2000", "amount": "80", "assignee_id": user.id},
        follow_redirects=False,
    )
    assert r.status_code == 303
    td = db.scalar(select(Todo))
    assert td.est_amount_gr == 8000 and td.assignee_id == user.id and td.done_on is None

    page = auth_client.get(f"/items/{fiat.id}").text
    assert "Wyszlifować reflektory" in page and "papier 2000" in page
    # visible on the parent zone and on the dashboard (with the item label)
    assert "Wyszlifować reflektory" in auth_client.get(f"/items/{cars.id}").text
    dash = auth_client.get("/").text
    assert "Wyszlifować reflektory" in dash  # tool tile preview
    assert "☐ 1" in dash  # badge on the Samochody tile
    page = auth_client.get("/do-zrobienia").text
    assert "Samochody › Fiat" in page and "Wyszlifować reflektory" in page

    r = auth_client.post(f"/todos/{td.id}/toggle?next=/", follow_redirects=False)
    assert r.headers["location"] == "/"
    db.refresh(td)
    assert td.done_on == today() and td.done_by_id == user.id
    assert "Nic na liście" in auth_client.get("/").text
    assert "Zrobione (1)" in auth_client.get(f"/items/{fiat.id}").text


def test_todo_next_must_be_local(auth_client, db):
    _, fiat = _car(auth_client, db)
    auth_client.post(f"/items/{fiat.id}/todos", data={"title": "x"})
    td = db.scalar(select(Todo))
    r = auth_client.post(f"/todos/{td.id}/toggle?next=//evil.example", follow_redirects=False)
    assert r.headers["location"].startswith(f"/items/{fiat.id}")


def test_todo_edit_and_plan(auth_client, db):
    _, fiat = _car(auth_client, db)
    auth_client.post(f"/items/{fiat.id}/todos", data={"title": "Lampy"})
    td = db.scalar(select(Todo))
    auth_client.post(f"/todos/{td.id}/edit", data={"title": "Lampy – polerka", "notes": "UV", "amount": "120,50"})
    db.refresh(td)
    assert td.title == "Lampy – polerka" and td.est_amount_gr == 12050

    on = today() + timedelta(days=10)
    auth_client.post(f"/todos/{td.id}/plan", data={"on": on.isoformat()})
    db.expire_all()
    assert db.scalar(select(Todo)) is None
    task = db.scalar(select(Task).where(Task.title == "Lampy – polerka"))
    assert task.next_due_date == on and task.amount_gr == 12050 and task.direction == "expense"
    assert task.category == "diy" and task.notes == "UV" and task.item_id == fiat.id


def test_todo_delete(auth_client, db):
    _, fiat = _car(auth_client, db)
    auth_client.post(f"/items/{fiat.id}/todos", data={"title": "x"})
    td = db.scalar(select(Todo))
    auth_client.post(f"/todos/{td.id}/delete")
    db.expire_all()
    assert db.scalar(select(Todo)) is None


def test_issue_on_car_and_zone(auth_client, db):
    cars, fiat = _car(auth_client, db)
    auth_client.post(f"/items/{fiat.id}/issues", data={"title": "Stuk przy skręcie"})
    assert db.scalar(select(Issue)).item_id == fiat.id
    page = auth_client.get(f"/items/{fiat.id}").text
    assert "Usterki i problemy" in page and "Stuk przy skręcie" in page
    assert "Stuk przy skręcie" in auth_client.get(f"/items/{cars.id}").text
    assert "1 sprawa" in auth_client.get("/").text


def test_journal(auth_client, db):
    _, fiat = _car(auth_client, db)
    auth_client.post(f"/items/{fiat.id}/journal", data={"body": "Lampy matowe", "on": "2026-11-02"})
    e = db.scalar(select(JournalEntry))
    assert str(e.noted_on) == "2026-11-02" and e.body == "Lampy matowe"
    assert "Lampy matowe" in auth_client.get(f"/items/{fiat.id}").text
    auth_client.post(f"/journal/{e.id}/edit", data={"body": "Lampy bardzo matowe", "on": "2026-11-01"})
    db.refresh(e)
    assert e.body == "Lampy bardzo matowe" and str(e.noted_on) == "2026-11-01"
    r = auth_client.post(f"/items/{fiat.id}/journal", data={"body": "  "})
    assert r.status_code == 400
    auth_client.post(f"/journal/{e.id}/delete")
    db.expire_all()
    assert db.scalar(select(JournalEntry)) is None


def test_pinned_notes(auth_client, db):
    _, fiat = _car(auth_client, db)
    auth_client.post(f"/items/{fiat.id}/notes", data={"notes": "Opony 225/45 R18"})
    db.refresh(fiat)
    assert fiat.notes == "Opony 225/45 R18"
    assert "Opony 225/45 R18" in auth_client.get(f"/items/{fiat.id}").text


def test_general_todo_and_plan_needs_item(auth_client, db):
    _, fiat = _car(auth_client, db)
    auth_client.post("/do-zrobienia", data={"title": "Umówić hydraulika"})
    td = db.scalar(select(Todo))
    assert td.item_id is None
    page = auth_client.get("/do-zrobienia").text
    assert "Ogólne" in page and "Umówić hydraulika" in page
    r = auth_client.post(f"/todos/{td.id}/toggle", follow_redirects=False)
    assert r.headers["location"] == "/do-zrobienia"
    auth_client.post(f"/todos/{td.id}/toggle")
    # planning a general todo requires choosing where it lives
    on = (today() + timedelta(days=3)).isoformat()
    r = auth_client.post(f"/todos/{td.id}/plan", data={"on": on})
    assert r.status_code == 400
    auth_client.post(f"/todos/{td.id}/plan", data={"on": on, "item_id": fiat.id})
    db.expire_all()
    assert db.scalar(select(Task).where(Task.title == "Umówić hydraulika")).item_id == fiat.id
    # moving a todo between items
    auth_client.post("/do-zrobienia", data={"title": "Lampy", "item_id": fiat.id})
    td = db.scalar(select(Todo).where(Todo.title == "Lampy"))
    auth_client.post(f"/todos/{td.id}/edit", data={"title": "Lampy", "item_id": ""})
    db.refresh(td)
    assert td.item_id is None
