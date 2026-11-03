"""Documents and photos kept with items, warranties with an end-date reminder."""

from __future__ import annotations

import io
import tarfile
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from domownik import documents, services
from domownik.config import get_settings, today
from domownik.models import Attachment, Completion, Item, Task, Warranty
from domownik.search import search

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    """Every test gets its own uploads folder (item ids repeat between test databases)."""
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")


def _item(db, name="Pralka 1", kind="appliance") -> Item:
    item = Item(name=name, kind=kind)
    db.add(item)
    db.commit()
    return item


def _jpeg(size=(300, 100), orientation: int | None = None) -> bytes:
    img = Image.new("RGB", size, (200, 30, 30))
    buf = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (40, 40), (0, 120, 0, 128)).save(buf, "PNG")
    return buf.getvalue()


def _pdf() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), "white").save(buf, "PDF")
    return buf.getvalue()


def _upload(client, item_id, files, **data):
    return client.post(
        f"/items/{item_id}/files",
        files=[("files", f) for f in files],
        data={"doc_kind": "", "description": "", **data},
        headers=HX,
        follow_redirects=False,
    )


def _atts(db) -> list[Attachment]:
    db.expire_all()
    return list(db.scalars(select(Attachment).order_by(Attachment.id)))


def _item_files(item_id: int) -> list[str]:
    folder = get_settings().files_dir / str(item_id)
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# ---------------------------------------------------------------- upload


def test_photo_upload_keeps_file_and_makes_upright_thumbnail(auth_client, db):
    item = _item(db)
    # landscape pixels, EXIF says "rotate 90°" – the way phones store portrait photos
    r = _upload(auth_client, item.id, [("IMG_0001.jpg", _jpeg((300, 100), orientation=6), "image/jpeg")])
    assert r.status_code == 303 and r.headers["HX-Redirect"] == f"/items/{item.id}#dokumenty"
    (att,) = _atts(db)
    assert att.content_type == "image/jpeg" and att.doc_kind == "photo" and att.has_thumb
    assert att.original_name == "IMG_0001.jpg" and att.created_by_id is not None
    path = documents.file_path(att)
    assert path.is_file() and path.parent.name == str(item.id)
    assert "IMG_0001" not in path.name  # random name on disk
    with Image.open(documents.thumb_path(att)) as th:
        assert th.format == "WEBP"
        assert th.height > th.width  # turned upright


def test_several_files_at_once_with_kind_and_description(auth_client, db):
    item = _item(db)
    r = _upload(
        auth_client,
        item.id,
        [("polisa.pdf", _pdf(), "application/pdf"), ("tabliczka.png", _png(), "image/png")],
        doc_kind="policy",
        description="Polisa 2026",
    )
    assert r.status_code == 303
    pdf, png = _atts(db)
    assert pdf.content_type == "application/pdf" and not pdf.has_thumb
    assert png.content_type == "image/png" and png.has_thumb
    assert {pdf.doc_kind, png.doc_kind} == {"policy"} and pdf.description == "Polisa 2026"


def test_download_headers(auth_client, db):
    item = _item(db)
    _upload(
        auth_client,
        item.id,
        [("Faktura ż.pdf", _pdf(), "application/pdf"), ("lista.csv", "a;b\nż;ó\n".encode(), "text/csv")],
    )
    pdf, csv = _atts(db)
    r = auth_client.get(f"/files/{pdf.id}")
    assert r.status_code == 200 and r.content == documents.file_path(pdf).read_bytes()
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline;")
    assert "filename*=utf-8''Faktura%20%C5%BC.pdf" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    r = auth_client.get(f"/files/{pdf.id}?dl=1")
    assert r.headers["content-disposition"].startswith("attachment;")
    r = auth_client.get(f"/files/{csv.id}")
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize(
    "name,data",
    [
        ("program.exe", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 100),
        ("zdjecie.jpg", b"to nie jest zdjecie, tylko tekst"),
        ("strona.html", b"<html><script>alert(1)</script></html>"),
        ("rysunek.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
        ("pusty.pdf", b""),
    ],
)
def test_wrong_type_is_rejected_by_content(auth_client, db, name, data):
    item = _item(db)
    r = _upload(auth_client, item.id, [(name, data, "image/jpeg")])
    assert r.status_code == 422 and r.headers["HX-Retarget"] == "#files-error"
    assert "error" in r.text
    assert _atts(db) == [] and _item_files(item.id) == []


def test_one_bad_file_stores_nothing(auth_client, db):
    item = _item(db)
    r = _upload(
        auth_client,
        item.id,
        [("ok.png", _png(), "image/png"), ("zly.jpg", b"\xff\xd8\xff" + b"zepsute" * 10, "image/jpeg")],
    )
    assert r.status_code == 422 and "uszkodzony" in r.text
    assert _atts(db) == [] and _item_files(item.id) == []


def test_too_big_file_is_rejected(auth_client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    item = _item(db)
    big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10)
    r = _upload(auth_client, item.id, [("duzy.pdf", big, "application/pdf")])
    assert r.status_code == 422 and "za duży" in r.text
    assert _atts(db) == [] and _item_files(item.id) == []


def test_no_file_picked(auth_client, db):
    item = _item(db)
    r = auth_client.post(f"/items/{item.id}/files", data={"doc_kind": ""}, headers=HX)
    assert r.status_code == 422 and "Wybierz plik" in r.text


def test_sniff_formats():
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
    assert documents.sniff(heic, "IMG_1.HEIC").mime == "image/heic"
    assert documents.sniff(b"PK\x03\x04rest", "umowa.docx").ext == "docx"
    assert documents.sniff("Zażółć gęślą".encode("cp1250"), "notatka.txt").mime == "text/plain"
    with pytest.raises(documents.UploadError):
        documents.sniff(b"PK\x03\x04rest", "skrypt.jar")


def test_clean_name():
    assert documents.clean_name("../../etc/passwd") == "passwd"
    assert documents.clean_name("C:\\Users\\x\\skan.pdf") == "skan.pdf"
    assert documents.clean_name("a\x00b\nc.pdf") == "abc.pdf"
    assert documents.clean_name("") == "plik"


# ---------------------------------------------------------------- access


def test_files_need_login(client, db):
    item = _item(db)
    att = Attachment(
        item_id=item.id,
        storage_path="files/1/" + "a" * 32 + ".pdf",
        original_name="x.pdf",
        content_type="application/pdf",
        size=1,
    )
    db.add(att)
    db.commit()
    for url in (f"/files/{att.id}", f"/files/{att.id}/thumb", f"/files/{att.id}/view"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.post(f"/items/{item.id}/files", files=[("files", ("a.png", _png(), "image/png"))])
    assert r.status_code in (401, 403) or "/login" in str(r.url)
    assert _atts(db) == [att]


def test_tampered_path_is_never_served(auth_client, db):
    item = _item(db)
    secret = get_settings().data_dir / "secrets" / "probe.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("tajne")
    for bad in ("../secrets/probe.txt", "files/1/../../secrets/probe.txt", "/etc/passwd", "files/1/x.txt"):
        att = Attachment(item_id=item.id, storage_path=bad, original_name="x", content_type="text/plain", size=1)
        db.add(att)
        db.commit()
        assert auth_client.get(f"/files/{att.id}").status_code == 404
        assert auth_client.get(f"/files/{att.id}/thumb").status_code == 404
    assert auth_client.get("/files/..%2F..%2Fsecrets%2Fprobe.txt").status_code in (404, 422)


# ---------------------------------------------------------------- edit / delete / page / search


def test_delete_removes_file_and_thumbnail(auth_client, db):
    item = _item(db)
    _upload(auth_client, item.id, [("a.jpg", _jpeg(), "image/jpeg")])
    (att,) = _atts(db)
    path, thumb = documents.file_path(att), documents.thumb_path(att)
    assert path.exists() and thumb.exists()
    r = auth_client.post(f"/files/{att.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert _atts(db) == [] and not path.exists() and not thumb.exists()


def test_edit_kind_and_description(auth_client, db):
    item = _item(db)
    _upload(auth_client, item.id, [("skan.pdf", _pdf(), "application/pdf")])
    (att,) = _atts(db)
    r = auth_client.post(
        f"/files/{att.id}/edit",
        data={"doc_kind": "manual", "description": "Instrukcja obsługi", "original_name": "../instrukcja.pdf"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    (att,) = _atts(db)
    assert (att.doc_kind, att.description, att.original_name) == ("manual", "Instrukcja obsługi", "instrukcja.pdf")


def test_item_page_shows_documents(auth_client, db):
    item = _item(db)
    _upload(auth_client, item.id, [("a.jpg", _jpeg(), "image/jpeg"), ("umowa.pdf", _pdf(), "application/pdf")])
    photo, pdf = _atts(db)
    html = auth_client.get(f"/items/{item.id}").text
    assert 'id="dokumenty"' in html
    assert f'src="/files/{photo.id}/thumb"' in html
    assert "umowa.pdf" in html and f'href="/files/{pdf.id}"' in html
    assert 'capture="environment"' in html and 'enctype="multipart/form-data"' in html
    # documents come after the tasks, before the journal
    assert html.index("<h2>Zadania</h2>") < html.index('id="dokumenty"') < html.index('id="dziennik"')
    win = auth_client.get(f"/files/{photo.id}/view", headers=HX).text
    assert "<html" not in win and f'src="/files/{photo.id}"' in win and "hx-confirm" in win
    page = auth_client.get(f"/files/{pdf.id}/view").text
    assert "<html" in page and "Pralka 1" in page


def test_search_finds_documents(auth_client, db):
    item = _item(db)
    _upload(auth_client, item.id, [("skan_01.pdf", _pdf(), "application/pdf")], doc_kind="policy", description="OC")
    db.add(Warranty(item_id=item.id, name="Gwarancja producenta", ends_on=today() + timedelta(days=400)))
    db.commit()
    assert [h.title for h in search(db, "skan").groups["doc"]]
    assert search(db, "polisa").groups["doc"][0].url.startswith("/files/")  # by kind
    assert search(db, "producenta").groups["doc"][0].url == f"/items/{item.id}#dokumenty"


# ---------------------------------------------------------------- invoice with a completion


def test_invoice_added_when_ticking_a_task_off(auth_client, db):
    item = _item(db, "Auto 1", "car")
    task = Task(item_id=item.id, title="Serwis", category="service", recurrence={"mode": "once"}, direction="none")
    task.next_due_date = today()
    db.add(task)
    db.commit()
    bad = auth_client.post(
        f"/tasks/{task.id}/done",
        data={"done_on": today().isoformat()},
        files=[("files", ("faktura.pdf", b"nie pdf", "application/pdf"))],
        headers=HX,
    )
    assert bad.status_code == 422 and bad.headers["HX-Retarget"] == "#done-error"
    db.expire_all()
    assert db.get(Task, task.id).active and not db.scalars(select(Completion)).all()
    r = auth_client.post(
        f"/tasks/{task.id}/done",
        data={"done_on": today().isoformat()},
        files=[("files", ("faktura.pdf", _pdf(), "application/pdf"))],
        headers=HX,
    )
    assert r.status_code == 200
    (att,) = _atts(db)
    comp = db.scalars(select(Completion)).one()
    assert att.completion_id == comp.id and att.doc_kind == "invoice" and att.item_id == item.id
    html = auth_client.get(f"/items/{item.id}").text
    assert 'class="h-file"' in html


# ---------------------------------------------------------------- backup


def test_backup_contains_uploaded_files(engine, tmp_path, monkeypatch):
    import sqlite3

    from typer.testing import CliRunner

    from domownik import cli

    s = get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path)
    sqlite3.connect(tmp_path / "db.sqlite").close()
    f = tmp_path / "uploads" / "files" / "7" / ("b" * 32 + ".pdf")
    f.parent.mkdir(parents=True)
    f.write_bytes(_pdf())
    (tmp_path / "uploads" / "files" / "7" / ("b" * 32 + ".thumb.webp")).write_bytes(b"x")
    result = CliRunner().invoke(cli.app, ["backup"])
    assert result.exit_code == 0, result.output
    (archive,) = (tmp_path / "backups" / "auto").glob("domownik-*.tar.gz")
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert f"uploads/files/7/{'b' * 32}.pdf" in names
        # and it unpacks with the same filter `domownik import` uses
        out = tmp_path / "restore"
        tar.extractall(out, filter="data")
    assert (out / "uploads" / "files" / "7" / f.name).read_bytes() == f.read_bytes()


# ---------------------------------------------------------------- warranties


def _warranty_task(db, w: Warranty) -> Task | None:
    wid = w.id
    db.expire_all()
    w = db.get(Warranty, wid)
    return db.get(Task, w.task_id) if w.task_id else None


def test_warranty_end_date_creates_and_moves_a_reminder(auth_client, db):
    item = _item(db)
    end = today() + timedelta(days=200)
    r = auth_client.post(
        f"/items/{item.id}/warranties",
        data={"name": "Gwarancja producenta", "starts_on": "", "ends_on": end.isoformat(), "notes": "5 lat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    w = db.scalars(select(Warranty)).one()
    task = _warranty_task(db, w)
    task_id = task.id
    assert task is not None and task.active and task.item_id == item.id
    assert (task.next_due_date, task.lead_days, task.category) == (end, 30, "warranty")
    assert task.title == "Koniec: Gwarancja producenta" and task.direction == "none"
    # the ordinary mechanisms see it: upcoming rows / timeline / notifications all read active tasks
    from domownik.domain.tree import Tree

    rows = services.task_rows(db, Tree.load(db), today(), {item.id})
    assert [r.task.id for r in rows] == [task.id]

    new_end = today() + timedelta(days=20)
    auth_client.post(f"/warranties/{w.id}/edit", data={"name": "Gwarancja", "ends_on": new_end.isoformat()})
    moved = _warranty_task(db, w)
    assert moved.id == task_id and moved.next_due_date == new_end and moved.title == "Koniec: Gwarancja"
    html = auth_client.get(f"/items/{item.id}").text
    assert "kończy się za 20 dni" in html

    auth_client.post(f"/warranties/{w.id}/edit", data={"name": "Gwarancja", "ends_on": ""})
    assert _warranty_task(db, w) is None and db.get(Task, task_id) is None
    assert "bezterminowo" in auth_client.get(f"/items/{item.id}").text


def test_acknowledged_reminder_stays_done_until_the_date_changes(db, user):
    item = _item(db)
    w = Warranty(item_id=item.id, name="Umowa serwisowa", ends_on=today() + timedelta(days=10))
    db.add(w)
    db.flush()
    documents.sync_reminder(db, w, today())
    db.commit()
    task = db.get(Task, w.task_id)
    services.complete_task(db, task, user=user, done_on=today(), amount_gr=None, km=None, vendor=None, notes=None)
    assert not task.active
    documents.sync_reminder(db, w, today())  # e.g. notes edited
    assert not task.active
    w.ends_on = today() + timedelta(days=375)  # renewed for a year
    documents.sync_reminder(db, w, today())
    db.commit()
    assert task.active and task.next_due_date == w.ends_on and w.task_id == task.id


def test_expired_warranty_gets_no_reminder_and_delete_removes_it(auth_client, db):
    item = _item(db)
    auth_client.post(
        f"/items/{item.id}/warranties", data={"name": "Stara gwarancja", "ends_on": "2020-01-31", "starts_on": ""}
    )
    old = db.scalars(select(Warranty)).one()
    assert old.task_id is None
    assert "wygasła" in auth_client.get(f"/items/{item.id}").text

    auth_client.post(
        f"/items/{item.id}/warranties",
        data={"name": "Umowa najmu", "ends_on": (today() + timedelta(days=90)).isoformat()},
    )
    w = db.scalars(select(Warranty).where(Warranty.name == "Umowa najmu")).one()
    task_id, wid = w.task_id, w.id
    assert task_id
    r = auth_client.post(f"/warranties/{wid}/delete", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Warranty, wid) is None and db.get(Task, task_id) is None


def test_warranty_validation(auth_client, db):
    item = _item(db)
    r = auth_client.post(f"/items/{item.id}/warranties", data={"name": ""}, headers=HX)
    assert r.status_code == 422 and r.headers["HX-Retarget"] == "#warranty-error"
    r = auth_client.post(
        f"/items/{item.id}/warranties",
        data={"name": "X", "starts_on": "2026-05-01", "ends_on": "2026-01-01"},
        headers=HX,
    )
    assert r.status_code == 422 and "wcześniejsza" in r.text
    assert db.scalars(select(Warranty)).all() == []


def test_editing_the_reminder_task_moves_the_warranty_end(auth_client, db):
    item = _item(db)
    w = Warranty(item_id=item.id, name="Gwarancja", ends_on=today() + timedelta(days=100))
    db.add(w)
    db.flush()
    documents.sync_reminder(db, w, today())
    db.commit()
    new = today() + timedelta(days=150)
    r = auth_client.post(
        f"/tasks/{w.task_id}/edit",
        data={"title": "Koniec: Gwarancja", "item_id": item.id, "freq": "once", "next_due_date": new.isoformat()},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Warranty, w.id).ends_on == new


def test_warranty_hint_from_template_attribute(auth_client, db):
    item = Item(name="Pompa 1", kind="appliance", attributes={"warranty_until": "05.2031"})
    db.add(item)
    db.commit()
    html = auth_client.get(f"/items/{item.id}").text
    assert "Gwarancja do: 05.2031" in html and 'value="2031-05-31"' in html
    assert documents.parse_loose_date("2031-05-31") == documents.parse_loose_date("31.05.2031")
    assert documents.parse_loose_date("2031").month == 12 and documents.parse_loose_date("kiedyś") is None


# ---------------------------------------------------------------- migration


def test_migration_0010_up_and_down(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")  # like `domownik migrate`
        conn.commit()
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "5c2e7a91d0b4")
            conn.execute(
                sa.text(
                    "INSERT INTO item (id, kind, name, sort_order, attributes, archived) VALUES (1, 'appliance', 'Pralka', 0, '{}', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO task (id, item_id, title, category, recurrence, lead_days, direction, autopay, active) "
                    "VALUES (1, 1, 'Filtr', 'diy', '{\"mode\": \"once\"}', 7, 'none', 0, 1)"
                )
            )
            command.upgrade(cfg, "head")
            conn.execute(
                sa.text(
                    "INSERT INTO attachment (id, item_id, storage_path, original_name, content_type, size, doc_kind, has_thumb) "
                    "VALUES (1, 1, 'files/1/x.pdf', 'x.pdf', 'application/pdf', 1, 'other', 0)"
                )
            )
            conn.execute(
                sa.text("INSERT INTO warranty (id, item_id, name, attachment_id, task_id) VALUES (1, 1, 'G', 1, 1)")
            )
            command.downgrade(cfg, "5c2e7a91d0b4")
            tables = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
            assert "attachment" not in tables and "warranty" not in tables
            assert conn.execute(sa.text("SELECT count(*) FROM task WHERE item_id = 1")).scalar() == 1
            command.upgrade(cfg, "head")
