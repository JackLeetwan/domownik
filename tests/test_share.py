"""📤 Udostępnij → Domownik (Web Share Target)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from domownik.models import Attachment, ShoppingItem, Todo
from domownik.routers.share import shared_text

MANIFEST = Path(__file__).parents[1] / "src/domownik/static/manifest.json"


def _no_csrf(client):
    client.headers.pop("X-CSRF-Token", None)


def test_manifest_points_here():
    t = json.loads(MANIFEST.read_text())["share_target"]
    assert t["action"] == "/udostepnij" and t["method"] == "POST" and t["enctype"] == "multipart/form-data"
    assert t["params"]["files"][0]["name"] == "files"


def test_shared_text_split():
    assert shared_text("", "Bigos – Kwestia Smaku https://www.kwestiasmaku.com/przepis/bigos", "") == (
        "Bigos – Kwestia Smaku",
        "https://www.kwestiasmaku.com/przepis/bigos",
    )
    assert shared_text("Bigos", "", "https://x.pl/bigos") == ("Bigos", "https://x.pl/bigos")
    assert shared_text("", "mleko\nchleb", "") == ("mleko\nchleb", None)


def test_link_asks_where_and_saves_nothing(auth_client, db):
    _no_csrf(auth_client)  # the share sheet has no token
    r = auth_client.post("/udostepnij", data={"text": "Zobacz https://www.kwestiasmaku.com/przepis/bigos"})
    assert r.status_code == 200
    assert (
        'action="/kuchnia/przepisy/z-linku"' in r.text
        and 'value="https://www.kwestiasmaku.com/przepis/bigos"' in r.text
    )
    assert "Do zrobienia" in r.text and "Notatka" in r.text
    assert db.query(Todo).count() == 0 and db.query(ShoppingItem).count() == 0


def test_list_goes_to_shopping(auth_client, db):
    from tests.conftest import csrf_of

    r = auth_client.post("/udostepnij", data={"text": "mleko\nchleb\n2 masło"})
    assert "każda linia osobno" in r.text
    auth_client.post("/zakupy", data={"text": "mleko\nchleb\n2 masło", "csrf": csrf_of(r.text)})
    assert sorted(s.name for s in db.query(ShoppingItem)) == ["Chleb", "Masło", "Mleko"]


def test_files_go_to_the_inbox(auth_client, db):
    _no_csrf(auth_client)
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(buf, "JPEG")
    r = auth_client.post(
        "/udostepnij", files={"files": ("faktura.jpg", buf.getvalue(), "image/jpeg")}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/zdjecia"
    att = db.query(Attachment).one()
    assert att.inbox


def test_foreign_site_and_signed_out(auth_client, db):
    _no_csrf(auth_client)
    r = auth_client.post("/udostepnij", data={"text": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    auth_client.cookies.clear()
    r = auth_client.post("/udostepnij", data={"text": "x"}, follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
