"""📝 Reading documents: queued with the photo, taken and answered by the worker on the PC through /api/ocr."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from PIL import Image

from domownik import documents, ocr
from domownik.config import get_settings
from domownik.models import Attachment, OcrJob

HX = {"HX-Request": "true"}
TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")
    monkeypatch.setattr(get_settings(), "ocr_token", TOKEN)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 400), (240, 240, 240)).save(buf, "JPEG")
    return buf.getvalue()


def _snap(client) -> Attachment:
    r = client.post("/zdjecia", files={"files": ("IMG_1.jpg", _jpeg(), "image/jpeg")}, headers=HX)
    assert r.status_code == 200, r.text
    return r


def _card(client) -> str:
    return client.get("/zdjecia").text


def test_photo_is_queued_and_waits(auth_client, db):
    _snap(auth_client)
    att = db.query(Attachment).one()
    job = db.query(OcrJob).one()
    assert job.attachment_id == att.id and job.status == "queued"
    page = _card(auth_client)
    assert "Czeka na odczyt" in page and f'hx-get="/zdjecia/{att.id}/odczyt"' in page
    assert "Czeka na odczyt" in auth_client.get(f"/zdjecia/{att.id}/odczyt", headers=HX).text


def test_pdf_is_queued(auth_client, db):
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    r = auth_client.post("/zdjecia", files={"files": ("polisa.pdf", pdf, "application/pdf")}, headers=HX)
    assert r.status_code == 200, r.text
    assert db.query(OcrJob).one().status == "queued"


def test_note_is_saved_without_rerender(auth_client, db):
    _snap(auth_client)
    att = db.query(Attachment).one()
    assert 'name="note"' in _card(auth_client)
    r = auth_client.post(f"/zdjecia/{att.id}/oznacz", data={"note": "  kolano Pauliny ", "mode": "card"}, headers=HX)
    assert r.status_code == 200
    db.refresh(att)
    assert att.description == "kolano Pauliny"
    assert 'value="kolano Pauliny"' in _card(auth_client)
    # a tap on a chip does not clear the note
    auth_client.post(f"/zdjecia/{att.id}/oznacz", data={"kind": "other", "mode": "card"}, headers=HX)
    db.refresh(att)
    assert att.description == "kolano Pauliny"


def test_api_needs_the_token(client, monkeypatch):
    assert client.post("/api/ocr/next").status_code == 401
    assert client.post("/api/ocr/next", headers={"Authorization": "Bearer zly"}).status_code == 401
    monkeypatch.setattr(get_settings(), "ocr_token", "")
    assert client.post("/api/ocr/next", headers=AUTH).status_code == 404


def test_worker_reads_and_answers(auth_client, client, db):
    _snap(auth_client)
    att = db.query(Attachment).one()
    r = client.post("/api/ocr/next", headers=AUTH)
    assert r.status_code == 200 and r.headers["X-Waiting"] == "1"
    job = r.json()
    assert job["content_type"] == "image/jpeg" and job["attempt"] == 1
    assert "Odczytywanie" in _card(auth_client)
    # nothing else to take meanwhile
    assert client.post("/api/ocr/next", headers=AUTH).status_code == 204

    f = client.get(f"/api/ocr/{job['id']}/plik", headers=AUTH)
    assert f.status_code == 200 and f.content == documents.file_path(att).read_bytes()

    r = client.post(
        f"/api/ocr/{job['id']}/wynik",
        json={"text": "PARAGON FISKALNY\nSUMA PLN 6,98", "source": "ocr", "model": "PaddleOCR-VL-1.6", "pages": 1},
        headers=AUTH,
    )
    assert r.json() == {"status": "done"}
    page = _card(auth_client)
    assert "Tekst z dokumentu" in page and "SUMA PLN 6,98" in page and '/odczyt"' not in page
    # an answer for a job no longer being read is refused
    assert client.post(f"/api/ocr/{job['id']}/wynik", json={"text": "x"}, headers=AUTH).status_code == 409
    assert client.get(f"/api/ocr/{job['id']}/plik", headers=AUTH).status_code == 409


def test_new_photos_before_old_documents(db, auth_client):
    _snap(auth_client)
    _snap(auth_client)
    old, new = db.query(OcrJob).order_by(OcrJob.id).all()
    old.priority = 10
    db.commit()
    assert ocr.claim(db).id == new.id


def test_lease_expires_and_attempts_run_out(auth_client, db):
    _snap(auth_client)
    for attempt in (1, 2, 3):
        job = ocr.claim(db)
        assert job is not None and job.attempts == attempt
        job.lease_until -= ocr.LEASE + timedelta(seconds=1)  # the PC went off mid-way
        db.commit()
    assert ocr.claim(db) is None
    db.commit()
    job = db.query(OcrJob).one()
    assert job.status == "failed" and job.error
    assert "Nie udało się odczytać" in _card(auth_client)


def test_worker_error_is_retried_then_fails(auth_client, client, db):
    _snap(auth_client)
    for attempt in (1, 2, 3):
        job = client.post("/api/ocr/next", headers=AUTH).json()
        r = client.post(f"/api/ocr/{job['id']}/wynik", json={"error": "model nie odpowiada"}, headers=AUTH)
        assert r.json()["status"] == ("failed" if attempt == 3 else "queued")
    assert "model nie odpowiada" in _card(auth_client)


def test_read_again(auth_client, db):
    _snap(auth_client)
    att = db.query(Attachment).one()
    job = ocr.claim(db)
    ocr.finish(db, job, text="zle", source="ocr", model="m", pages=1, error=None)
    db.commit()
    r = auth_client.post(f"/zdjecia/{att.id}/odczytaj", headers=HX)
    assert r.status_code == 200 and "Czeka na odczyt" in r.text
    db.refresh(job)
    assert job.status == "queued" and job.attempts == 0


def test_photo_from_before_the_queue_can_be_read(auth_client, db):
    _snap(auth_client)
    db.query(OcrJob).delete()
    db.commit()
    att = db.query(Attachment).one()
    auth_client.post(f"/zdjecia/{att.id}/odczytaj", headers=HX)
    assert db.query(OcrJob).one().status == "queued"


def test_deleting_the_photo_drops_the_job(auth_client, db):
    _snap(auth_client)
    att = db.query(Attachment).one()
    auth_client.post(f"/zdjecia/{att.id}/usun", headers=HX)
    db.expire_all()
    assert db.query(OcrJob).count() == 0


def test_worker_gives_a_job_back(auth_client, client, db):
    _snap(auth_client)
    job = client.post("/api/ocr/next", headers=AUTH).json()
    r = client.post(f"/api/ocr/{job['id']}/wynik", json={"release": True}, headers=AUTH)
    assert r.json() == {"status": "queued"}
    again = client.post("/api/ocr/next", headers=AUTH).json()
    assert again["id"] == job["id"] and again["attempt"] == 1
