"""Photos of recipes: the cookbook page or the dish as served – added on the recipe page or from 📥 Do rozpoznania,
shown as thumbnails (the last one as the list's cover), removed with the recipe (files too)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from domownik import documents
from domownik.config import get_settings
from domownik.models import Attachment, Recipe

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (200, 120, 40)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def recipe(db):
    r = Recipe(name="Crêpes Suzette", ingredients="300 g twarogu")
    db.add(r)
    db.commit()
    return r


def test_add_show_delete_photos(auth_client, db, recipe):
    page = auth_client.get(f"/kuchnia/przepisy/{recipe.id}").text
    assert 'id="recipe-photos"' in page and "Dodaj zdjęcie" in page

    r = auth_client.post(
        f"/kuchnia/przepisy/{recipe.id}/zdjecia",
        files=[("files", ("strona.jpg", _jpeg(), "image/jpeg")), ("files", ("danie.jpg", _jpeg(), "image/jpeg"))],
        headers=HX,
    )
    assert r.status_code == 200 and r.text.count('class="note-photo"') == 2
    first, second = db.query(Attachment).order_by(Attachment.id).all()
    assert first.recipe_id == recipe.id and first.item_id is None and first.has_thumb
    path = documents.file_path(first)
    assert path.is_file()
    assert auth_client.get(f"/files/{first.id}").status_code == 200
    assert auth_client.get(f"/files/{first.id}/view").status_code == 404  # item documents only

    listing = auth_client.get("/kuchnia/przepisy").text
    assert f'src="/files/{second.id}/thumb"' in listing and "📷 2" in listing  # the newest one is the cover

    r = auth_client.post(f"/kuchnia/przepisy/{recipe.id}/zdjecia/{first.id}/usun", headers=HX)
    assert r.text.count('class="note-photo"') == 1 and not path.exists()

    other = Recipe(name="Inny")
    db.add(other)
    db.commit()
    assert auth_client.post(f"/kuchnia/przepisy/{other.id}/zdjecia/{second.id}/usun", headers=HX).status_code == 404

    # deleting the recipe takes the remaining file with it
    second_path = documents.file_path(second)
    auth_client.post(f"/kuchnia/przepisy/{recipe.id}/usun")
    db.expire_all()
    assert db.query(Attachment).count() == 0 and not second_path.exists()


def test_bad_file_saves_nothing(auth_client, db, recipe):
    r = auth_client.post(
        f"/kuchnia/przepisy/{recipe.id}/zdjecia", files={"files": ("x.jpg", b"not an image", "image/jpeg")}, headers=HX
    )
    assert r.status_code == 400
    assert db.query(Attachment).count() == 0


def _snap(client) -> None:
    r = client.post("/zdjecia", files={"files": ("IMG_1.jpg", _jpeg(), "image/jpeg")}, headers=HX)
    assert r.status_code == 200, r.text


def test_inbox_photo_to_existing_recipe(auth_client, db, recipe):
    _snap(auth_client)
    att = db.query(Attachment).one()
    r = auth_client.post(f"/zdjecia/{att.id}/oznacz", data={"kind": "recipe", "mode": "card"}, headers=HX)
    assert "Do którego przepisu" in r.text and "Crêpes Suzette" in r.text  # no item needed for a recipe

    r = auth_client.post(f"/zdjecia/{att.id}/zapisz", data={"recipe_id": recipe.id}, headers=HX)
    assert r.status_code == 200
    db.expire_all()
    att = db.get(Attachment, att.id)
    assert not att.inbox and att.recipe_id == recipe.id and att.item_id is None
    assert 'class="note-photo"' in auth_client.get(f"/kuchnia/przepisy/{recipe.id}").text


def test_inbox_photo_to_new_recipe(auth_client, db):
    _snap(auth_client)
    att = db.query(Attachment).one()
    auth_client.post(f"/zdjecia/{att.id}/oznacz", data={"kind": "recipe", "mode": "card"}, headers=HX)

    r = auth_client.post(f"/zdjecia/{att.id}/zapisz", data={"recipe_id": "", "recipe_name": " "}, headers=HX)
    assert r.status_code == 400 and "nazwę" in r.text

    auth_client.post(f"/zdjecia/{att.id}/zapisz", data={"recipe_id": "", "recipe_name": "Pierogi babci"}, headers=HX)
    db.expire_all()
    (rec,) = db.query(Recipe).all()
    assert rec.name == "Pierogi babci" and [a.id for a in rec.photos] == [att.id]
