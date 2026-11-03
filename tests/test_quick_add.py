"""Quick add ("＋"): the window's forms reuse the ordinary routes and answer with an event."""

from __future__ import annotations

import json

from sqlalchemy import select

from domownik.models import BoardNote, BudgetCategory, Item, ShoppingItem, Task, Todo, Transaction

HX = {"HX-Request": "true"}
QUICK = {"HX-Request": "true", "X-Quick-Add": "1"}


def _added(r) -> str:
    assert r.status_code == 200, r.text
    assert r.headers.get("HX-Reswap") == "none"
    return json.loads(r.headers["HX-Trigger"])["dm-added"]


def test_menu_and_forms_render(auth_client, db):
    car = Item(name="Auto 1", kind="car")
    db.add(car)
    db.commit()
    r = auth_client.get("/dodaj", headers=HX)
    assert r.status_code == 200
    for label in ("Zadanie", "Zakup", "Notatka", "Do zrobienia", "Wydatek"):
        assert label in r.text
    for kind in ("task", "shopping", "note", "todo", "expense"):
        r = auth_client.get(f"/dodaj/{kind}", headers=HX)
        assert r.status_code == 200, kind
        assert "X-Quick-Add" in r.text and 'id="qa-error"' in r.text
    assert auth_client.get("/dodaj/nope", headers=HX).status_code == 404


def test_item_of_current_page_is_preselected(auth_client, db):
    a, b = Item(name="Auto 1", kind="car"), Item(name="Dom", kind="house")
    db.add_all([a, b])
    db.commit()
    r = auth_client.get("/dodaj/task", headers=HX | {"HX-Current-URL": f"http://testserver/items/{b.id}"})
    assert f'<option value="{b.id}" selected>' in r.text
    assert f"/tasks/new?item={b.id}" in r.text  # "Więcej opcji" goes to the full form of that item
    r = auth_client.get("/dodaj/task", headers=HX | {"HX-Current-URL": "http://testserver/zakupy"})
    assert "selected>" not in r.text.split('name="item_id"')[1].split("</select>")[0]


def test_quick_task_saves_with_guessed_category(auth_client, db):
    car = Item(name="Auto 1", kind="car")
    db.add(car)
    db.commit()
    r = auth_client.post(
        "/tasks/new",
        data={
            "title": "Wymiana oleju",
            "item_id": car.id,
            "freq": "once",
            "next_due_date": "2030-05-01",
            "amount": "250",
        },
        headers=QUICK,
    )
    assert _added(r) == "Dodano zadanie"
    task = db.scalar(select(Task).where(Task.title == "Wymiana oleju"))
    assert task.category == "service"  # like a new task of a car in the full form
    assert task.amount_gr == 25000 and task.direction == "expense"
    assert task.recurrence == {"mode": "once"}


def test_quick_task_error_goes_to_window(auth_client, db):
    car = Item(name="Auto 1", kind="car")
    db.add(car)
    db.commit()
    r = auth_client.post("/tasks/new", data={"title": "X", "item_id": car.id, "freq": "once"}, headers=QUICK)
    assert r.status_code == 422
    assert r.headers["HX-Retarget"] == "#qa-error"
    # the full form keeps its own error box
    r = auth_client.post("/tasks/new", data={"title": "X", "item_id": car.id, "freq": "once"}, headers=HX)
    assert r.headers["HX-Retarget"] == "#form-error"


def test_quick_shopping_note_todo(auth_client, db):
    assert _added(auth_client.post("/zakupy", data={"text": "mleko, 2 chleb"}, headers=QUICK)) == "Dopisano do zakupów"
    assert {s.name for s in db.scalars(select(ShoppingItem))} == {"Mleko", "Chleb"}

    assert _added(auth_client.post("/notatki", data={"body": "Paczka w piątek", "pinned": "on"}, headers=QUICK))
    note = db.scalar(select(BoardNote))
    assert note.body == "Paczka w piątek" and note.pinned

    home = Item(name="Dom", kind="house")
    db.add(home)
    db.commit()
    r = auth_client.post("/do-zrobienia", data={"title": "Uszczelka", "item_id": home.id}, headers=QUICK)
    assert _added(r) == "Dodano do zrobienia"
    assert db.scalar(select(Todo)).item_id == home.id


def test_quick_expense(auth_client, db):
    cat = BudgetCategory(name="Jedzenie", kind="variable")
    db.add(cat)
    db.commit()
    r = auth_client.post(
        "/budzet/wpis", data={"amount": "54,90", "budget_category_id": cat.id, "note": "sklep"}, headers=QUICK
    )
    assert _added(r) == "Dodano wydatek"
    tx = db.scalar(select(Transaction))
    assert tx.amount_gr == -5490 and tx.note == "sklep"
    r = auth_client.post("/budzet/wpis", data={"amount": "", "budget_category_id": cat.id}, headers=QUICK)
    assert r.status_code == 422 and r.headers["HX-Retarget"] == "#qa-error"


def test_ordinary_forms_still_redirect(auth_client):
    r = auth_client.post("/notatki", data={"body": "Zwykła"}, headers=HX, follow_redirects=False)
    assert r.status_code == 303 and r.headers["HX-Redirect"] == "/notatki"


def test_fab_on_pages_but_not_over_big_forms(auth_client, db):
    car = Item(name="Auto 1", kind="car")
    db.add(car)
    db.commit()
    assert 'class="fab"' in auth_client.get("/").text
    assert 'class="side-add"' in auth_client.get("/").text
    assert 'class="fab"' in auth_client.get(f"/items/{car.id}").text
    assert 'class="fab"' not in auth_client.get(f"/items/{car.id}/edit").text
    assert 'class="fab"' not in auth_client.get(f"/tasks/new?item={car.id}").text
