"""Photos on notes: added in the form or on the card, shown as thumbnails, removed with the note (files too),
private with a private note."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from domownik import documents
from domownik.config import get_settings
from domownik.models import Attachment, BoardNote, User
from domownik.security import hash_password

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (30, 90, 160)).save(buf, "JPEG")
    return buf.getvalue()


def test_add_with_photo_only_card_and_delete(auth_client, db):
    r = auth_client.post(
        "/notatki", data={"title": "", "body": ""}, files={"files": ("plan.jpg", _jpeg(), "image/jpeg")}
    )
    assert r.status_code in (200, 303)
    n = db.query(BoardNote).one()
    (att,) = n.photos
    assert att.item_id is None and att.note_id == n.id and att.has_thumb and att.storage_path.startswith("files/0/")
    path = documents.file_path(att)
    assert path.is_file()

    page = auth_client.get("/notatki").text
    assert f'src="/files/{att.id}/thumb"' in page
    assert auth_client.get(f"/files/{att.id}").status_code == 200
    assert auth_client.get(f"/files/{att.id}/view").status_code == 404  # item documents only

    # one more from the card
    card = auth_client.post(f"/notatki/{n.id}/zdjecia", files={"files": ("b.jpg", _jpeg(), "image/jpeg")}, headers=HX)
    assert card.status_code == 200 and card.text.count('class="note-photo"') == 2

    # removing one photo
    card = auth_client.post(f"/notatki/{n.id}/zdjecia/{att.id}/usun", headers=HX)
    assert card.text.count('class="note-photo"') == 1 and not path.exists()

    # deleting the (archived) note takes the remaining file with it
    db.expire_all()
    other = db.query(Attachment).one()
    other_path = documents.file_path(other)
    auth_client.post(f"/notatki/{n.id}/archive")
    auth_client.post(f"/notatki/{n.id}/delete")
    db.expire_all()
    assert db.query(Attachment).count() == 0 and not other_path.exists()


def test_bad_file_saves_nothing(auth_client, db):
    r = auth_client.post(
        "/notatki", data={"body": "Kod do furtki"}, files={"files": ("x.jpg", b"not an image at all", "image/jpeg")}
    )
    assert r.status_code == 422
    assert db.query(BoardNote).count() == 0 and db.query(Attachment).count() == 0


def test_photo_of_private_note_hidden(auth_client, db):
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.commit()
    n = BoardNote(body="Prezent", private_to_id=other.id, created_by_id=other.id)
    db.add(n)
    db.flush()
    (att,) = documents.store_files(db, None, [("p.jpg", io.BytesIO(_jpeg()))], user=other, note_id=n.id)
    db.commit()
    assert auth_client.get(f"/files/{att.id}").status_code == 404
    assert auth_client.get(f"/files/{att.id}/thumb").status_code == 404


def test_migration_0027(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn, conn.begin():
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        cols = {r[1]: r for r in conn.execute(sa.text('PRAGMA table_info("attachment")'))}
        assert "note_id" in cols and cols["item_id"][3] == 0  # notnull flag off
        command.downgrade(cfg, "5b8e2f4a7c13")
        cols = {r[1]: r for r in conn.execute(sa.text('PRAGMA table_info("attachment")'))}
        assert "note_id" not in cols and cols["item_id"][3] == 1
        command.upgrade(cfg, "head")
