"""Photos / files picked in a task's form: kept with the item, shown in the task window, survive the task."""

from __future__ import annotations

import io

from PIL import Image
from sqlalchemy import select

from domownik.models import Attachment, Item, Task

HX = {"HX-Request": "true"}


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (0, 120, 0)).save(buf, "PNG")
    return buf.getvalue()


def _item(db) -> Item:
    it = Item(name="Dom", kind="house")
    db.add(it)
    db.commit()
    return it


FORM = {"title": "Naprawić rynnę", "freq": "once", "next_due_date": "2030-05-01", "lead_days": "7"}


def test_new_task_with_photo(auth_client, db):
    item = _item(db)
    assert 'enctype="multipart/form-data"' in auth_client.get(f"/tasks/new?item={item.id}").text
    r = auth_client.post(
        "/tasks/new",
        data=FORM | {"item_id": item.id},
        files={"files": ("rynna.png", _png(), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    task = db.scalar(select(Task))
    att = db.scalar(select(Attachment))
    assert att.task_id == task.id and att.item_id == item.id and att.doc_kind == "photo"
    modal = auth_client.get(f"/tasks/{task.id}", headers=HX).text
    assert f"/files/{att.id}/thumb" in modal
    assert auth_client.get(f"/files/{att.id}/thumb").status_code == 200

    # files stay in the item's documents when the task goes
    auth_client.post(f"/tasks/{task.id}/delete")
    db.expire_all()
    left = db.scalar(select(Attachment))
    assert left is not None and left.task_id is None


def test_bad_file_saves_nothing(auth_client, db):
    item = _item(db)
    r = auth_client.post(
        "/tasks/new",
        data=FORM | {"item_id": item.id},
        files={"files": ("pusty.png", b"", "image/png")},
        headers=HX,
    )
    assert r.status_code == 422 and "pusty" in r.text
    assert db.scalar(select(Task)) is None and db.scalar(select(Attachment)) is None


def test_edit_adds_photo(auth_client, db):
    item = _item(db)
    auth_client.post("/tasks/new", data=FORM | {"item_id": item.id})
    task = db.scalar(select(Task))
    r = auth_client.post(
        f"/tasks/{task.id}/edit",
        data=FORM | {"item_id": item.id},
        files={"files": ("a.png", _png(), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.scalar(select(Attachment.task_id)) == task.id
    assert "Już dołączone: 1" in auth_client.get(f"/tasks/{task.id}/edit").text


def test_migration_0018(tmp_path):
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
            fks = {r[3] for r in conn.execute(sa.text("PRAGMA foreign_key_list(attachment)"))}
            assert "task_id" in fks
            command.downgrade(cfg, "b5e7c2d9a614")
            cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(attachment)"))}
            assert "task_id" not in cols and "completion_id" in cols
            command.upgrade(cfg, "head")
