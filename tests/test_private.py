"""'🔒 Tylko dla mnie': someone else's private tasks / notes / todos never show up – lists, item page,
dashboard, calendar, timeline, search, ICS, push, direct links; the shared budget counts them masked."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from domownik import ics, notify
from domownik.config import today
from domownik.models import BoardNote, BudgetCategory, Item, PushSubscription, Task, Todo, User
from domownik.security import hash_password

HX = {"HX-Request": "true"}
SECRET = "Prezent-niespodzianka"


@pytest.fixture
def other(db) -> User:
    u = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def data(db, other):
    """Ola's private task / note / todo + a shared task, in a house zone."""
    house = Item(name="Dom", kind="house")
    cat = BudgetCategory(name="Prezenty", kind="planned")
    db.add_all([house, cat])
    db.commit()
    d = today() + timedelta(days=2)
    common = {
        "item_id": house.id,
        "category": "occasion",
        "recurrence": {"mode": "once"},
        "next_due_date": d,
        "lead_days": 7,
    }
    secret = Task(
        title=SECRET, private_to_id=other.id, amount_gr=30000, direction="expense", budget_category_id=cat.id, **common
    )
    shared = Task(title="Wspólne zadanie", direction="none", **common)
    db.add_all(
        [
            secret,
            shared,
            BoardNote(body=f"Notatka {SECRET}", private_to_id=other.id, created_by_id=other.id),
            Todo(title=f"Todo {SECRET}", private_to_id=other.id, item_id=house.id),
        ]
    )
    db.commit()
    return {"house": house, "secret": secret, "shared": shared, "day": d}


def test_hidden_everywhere_for_others(auth_client, db, data):
    house, secret, d = data["house"], data["secret"], data["day"]
    pages = [
        "/",
        f"/items/{house.id}",
        "/notatki",
        "/do-zrobienia",
        f"/kalendarz?d={d.isoformat()}",
        f"/api/timeline?start={today().isoformat()}&end={(today() + timedelta(days=30)).isoformat()}",
    ]
    for url in pages:
        r = auth_client.get(url)
        assert r.status_code == 200, url
        assert SECRET not in r.text, url
    assert "Wspólne zadanie" in auth_client.get(f"/items/{house.id}").text
    found = auth_client.get(f"/szukaj?q={SECRET}").text  # the query itself is echoed – look at the hits
    assert f"/tasks/{secret.id}" not in found and f"Notatka {SECRET}" not in found and f"Todo {SECRET}" not in found
    assert auth_client.get(f"/tasks/{secret.id}", headers=HX).status_code == 404
    assert auth_client.get(f"/tasks/{secret.id}/edit").status_code == 404


def test_budget_counts_but_masks(auth_client, db, data):
    page = auth_client.get("/budzet").text
    assert SECRET not in page and "🔒 Prywatne" in page and "300" in page


def test_owner_sees_and_marks(client, db, data, other):
    from tests.conftest import csrf_of

    page = client.get("/login")
    client.post("/login", data={"email": "ola@example.com", "password": "tajnehaslo2", "csrf": csrf_of(page.text)})
    item = client.get(f"/items/{data['house'].id}").text
    assert SECRET in item and "🔒" in item
    assert f"Notatka {SECRET}" in client.get("/notatki").text
    assert SECRET in client.get("/budzet").text


def test_forms_set_private_and_refuse_giving_away(auth_client, db, data, user, other):
    house = data["house"]
    base = {
        "title": "Moje",
        "item_id": house.id,
        "freq": "once",
        "next_due_date": "2030-01-01",
        "lead_days": "1",
        "private_field": "1",
    }
    auth_client.post("/tasks/new", data=base | {"private": "1"})
    t = db.scalar(select(Task).where(Task.title == "Moje"))
    assert t.private_to_id == user.id
    r = auth_client.post(
        "/tasks/new", data=base | {"title": "Cudze", "private": "1", "assignee_id": other.id}, headers=HX
    )
    assert r.status_code == 422 and "Prywatne" in r.text
    # quick forms without the switch keep it as it was
    auth_client.post(
        f"/tasks/{t.id}/edit", data={k: v for k, v in base.items() if k != "private_field"} | {"title": "Moje 2"}
    )
    db.refresh(t)
    assert t.private_to_id == user.id
    auth_client.post(f"/tasks/{t.id}/edit", data=base)  # switch off
    db.refresh(t)
    assert t.private_to_id is None

    auth_client.post("/notatki", data={"body": "tajne", "private_field": "1", "private": "1"})
    assert db.scalar(select(BoardNote).where(BoardNote.body == "tajne")).private_to_id == user.id
    auth_client.post("/do-zrobienia", data={"title": "tajne todo", "private_field": "1", "private": "1"})
    assert db.scalar(select(Todo).where(Todo.title == "tajne todo")).private_to_id == user.id


def test_push_and_ics_skip_others_private(db, data, user, other, monkeypatch):
    sent = []
    monkeypatch.setattr("domownik.webpush.deliver", lambda sub, d, urgent=False: sent.append((sub.user_id, d)) or True)
    for u in (user, other):
        db.add(PushSubscription(user_id=u.id, endpoint=f"https://p.example/{u.id}", p256dh="k", auth="a"))
    db.commit()
    notify.run_slot(db, "morning", today())
    mine = [d for uid, d in sent if uid == user.id]
    hers = [d for uid, d in sent if uid == other.id]
    assert mine and SECRET not in " ".join(mine)
    assert hers and SECRET in " ".join(hers)

    db.info.pop("viewer_id", None)
    text = ics.build(db, user, today(), datetime.now())
    assert SECRET not in text and "Wspólne zadanie" in text
    db.info.pop("viewer_id", None)
    assert SECRET in ics.build(db, other, today(), datetime.now())


def test_todo_plan_keeps_private(auth_client, db, user):
    house = Item(name="Dom", kind="house")
    db.add(house)
    db.commit()
    td = Todo(title="Moje sprawy", private_to_id=user.id, item_id=house.id)
    db.add(td)
    db.commit()
    auth_client.post(f"/todos/{td.id}/plan", data={"on": "2030-01-01"})
    assert db.scalar(select(Task).where(Task.title == "Moje sprawy")).private_to_id == user.id


def test_migration_0023(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            for t in ("task", "board_note", "todo"):
                assert "private_to_id" in {r[3] for r in conn.execute(sa.text(f'PRAGMA foreign_key_list("{t}")'))}
            command.downgrade(cfg, "2a7f4c9e6d15")
            for t in ("task", "board_note", "todo"):
                assert "private_to_id" not in {r[1] for r in conn.execute(sa.text(f'PRAGMA table_info("{t}")'))}
            command.upgrade(cfg, "head")
