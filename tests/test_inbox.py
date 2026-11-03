"""📥 Do rozpoznania: a photo in one tap, two quick taps, the rest at home – service, tyres, receipt, policy."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

import pytest
from PIL import Image

from domownik import documents, inbox
from domownik.config import get_settings, today
from domownik.models import Attachment, BudgetCategory, Completion, Item, Task, Transaction, Warranty

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 400), (240, 240, 240)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def car(db):
    cars = Item(name="Samochody", kind="group")
    db.add(cars)
    db.flush()
    c = Item(name="Kombi", kind="car", parent_id=cars.id)
    db.add(c)
    db.add(BudgetCategory(name="Samochody", kind="planned"))
    db.commit()
    return c


def _snap(client, headers=HX, **data) -> Attachment:
    r = client.post("/zdjecia", data=data, files={"files": ("IMG_1.jpg", _jpeg(), "image/jpeg")}, headers=headers)
    assert r.status_code in (200, 303), r.text
    return r


def test_snap_and_two_taps(auth_client, db, car):
    menu = auth_client.get("/dodaj", headers=HX).text
    assert "Zdjęcie / dokument" in menu and 'capture="environment"' in menu

    r = _snap(auth_client)
    assert "Czego dotyczy?" in r.text and "Co to?" in r.text and "Kombi" in r.text
    att = db.query(Attachment).one()
    assert att.inbox and att.item_id is None and att.has_thumb
    assert inbox.count(db) == 1
    assert 'href="/zdjecia"' in auth_client.get("/").text

    r = auth_client.post(f"/zdjecia/{att.id}/oznacz", data={"item_id": car.id, "mode": "modal"}, headers=HX)
    assert 'aria-pressed="true"' in r.text and "Gotowe" in r.text
    r = auth_client.post(f"/zdjecia/{att.id}/oznacz", data={"kind": "service", "mode": "modal"}, headers=HX)
    assert r.text.count('aria-pressed="true"') == 2
    db.expire_all()
    assert (att.item_id, att.capture_kind) == (car.id, "service")

    assert auth_client.post(f"/zdjecia/{att.id}/oznacz", data={"kind": "zle"}, headers=HX).status_code == 400
    assert auth_client.post(f"/zdjecia/{att.id}/oznacz", data={"item_id": "999"}, headers=HX).status_code == 400


def test_snapped_on_an_item_page_knows_the_item(auth_client, db, car):
    _snap(auth_client, headers=HX | {"HX-Current-URL": f"http://testserver/items/{car.id}"})
    assert db.query(Attachment).one().item_id == car.id


def test_service_ticks_off_the_existing_task(auth_client, db, car):
    oil = Task(
        item_id=car.id,
        title="Wymiana oleju",
        category="service",
        recurrence={"mode": "interval", "every": 1, "unit": "year", "anchor": "completion"},
        next_due_date=today() + timedelta(days=40),
        amount_gr=60000,
    )
    db.add(oil)
    db.commit()
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, item_id=car.id, kind="service")
    db.commit()

    page = auth_client.get("/zdjecia").text
    assert f'<option value="{oil.id}" selected' in page and "jak w zadaniu" in page

    done_on = today() - timedelta(days=1)
    r = auth_client.post(
        f"/zdjecia/{att.id}/zapisz",
        data={
            "item_id": car.id,
            "task_id": oil.id,
            "done_on": done_on.isoformat(),
            "amount": "850",
            "next": "keep",
            "plan_next": "1",
        },
        headers=HX,
    )
    assert r.status_code == 200 and "Następny raz" in json.loads(r.headers["HX-Trigger"])["dm-added"]
    db.expire_all()
    comp = db.query(Completion).one()
    assert comp.task_id == oil.id and comp.done_on == done_on and comp.actual_amount_gr == 85000
    assert oil.next_due_date == done_on.replace(year=done_on.year + 1) and oil.amount_gr == 89000  # 850 × 1.05 ≈ 890
    att = db.get(Attachment, att.id)
    assert not att.inbox and att.completion_id == comp.id and att.doc_kind == "invoice"
    assert db.query(Transaction).one().amount_gr == -85000
    assert "Nic nie czeka" in auth_client.get("/zdjecia").text


def test_tyres_create_a_task_four_years_on(auth_client, db, car):
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, item_id=car.id, kind="tyres")
    db.commit()
    page = auth_client.get("/zdjecia").text
    assert 'value="4y" checked' in page and "jak w zadaniu" not in page

    d = date(2026, 4, 10)
    auth_client.post(
        f"/zdjecia/{att.id}/zapisz",
        data={
            "item_id": car.id,
            "task_id": "",
            "title": "Wymiana opon",
            "done_on": d.isoformat(),
            "amount": "2400",
            "next": "4y",
            "plan_next": "1",
        },
        headers=HX,
    )
    t = db.query(Task).one()
    assert t.title == "Wymiana opon" and t.active and t.next_due_date == date(2030, 4, 10)
    assert t.recurrence["every"] == 4 and t.recurrence["anchor"] == "completion" and t.amount_gr == 252000


def test_a_far_seasonal_swap_is_not_preselected(auth_client, db, car):
    """A tyre photo in November must not tick off next April's swap – it is a new set of tyres."""
    spring = Task(
        item_id=car.id,
        title="Zmiana opon na letnie",
        category="service",
        recurrence={"mode": "fixed", "dates": ["04-10"]},
        next_due_date=today() + timedelta(days=150),
    )
    db.add(spring)
    db.commit()
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, item_id=car.id, kind="tyres")
    db.commit()
    page = auth_client.get("/zdjecia").text
    assert f'<option value="{spring.id}" selected' not in page and '<option value="" selected' in page
    assert 'value="4y" checked' in page

    spring.next_due_date = today() + timedelta(days=10)  # the swap is due now: that is the one
    db.commit()
    assert f'<option value="{spring.id}" selected' in auth_client.get("/zdjecia").text


def test_a_service_ticked_off_already_gets_the_photo(auth_client, db, car):
    """Ticked off two days ago, the printout photographed later: attached, not ticked off a second time."""
    from domownik import services

    oil = Task(
        item_id=car.id,
        title="Wymiana oleju",
        category="service",
        recurrence={"mode": "interval", "every": 1, "unit": "year", "anchor": "completion"},
        next_due_date=today(),
    )
    db.add(oil)
    db.commit()
    comp = services.complete_task(
        db, oil, user=None, done_on=today() - timedelta(days=2), amount_gr=None, km=None, vendor=None, notes=None
    )
    due_after = oil.next_due_date
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, item_id=car.id, kind="service")
    db.commit()
    page = auth_client.get("/zdjecia").text
    assert f'<option value="c{comp.id}" selected' in page

    r = auth_client.post(
        f"/zdjecia/{att.id}/zapisz",
        data={"item_id": car.id, "task_id": f"c{comp.id}", "amount": "600", "next": "keep", "plan_next": "1"},
        headers=HX,
    )
    assert r.status_code == 200 and "dołączone" in json.loads(r.headers["HX-Trigger"])["dm-added"]
    db.expire_all()
    assert db.query(Completion).count() == 1 and db.get(Completion, comp.id).actual_amount_gr == 60000
    assert db.query(Transaction).one().amount_gr == -60000  # the missing cost, booked once
    assert db.get(Task, oil.id).next_due_date == due_after and db.get(Task, oil.id).amount_gr == 63000
    a = db.get(Attachment, att.id)
    assert not a.inbox and a.completion_id == comp.id


@pytest.fixture
def oc(db, car):
    t = Task(
        item_id=car.id,
        title="Ubezpieczenie OC",
        category="insurance",
        recurrence={"mode": "interval", "every": 1, "unit": "year", "anchor": "due"},
        next_due_date=today() + timedelta(days=20),
        amount_gr=60000,
    )
    db.add(t)
    db.commit()
    return t


def test_policy_renews_the_known_insurance(auth_client, db, car, oc):
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, item_id=car.id, kind="policy")
    db.commit()
    page = auth_client.get("/zdjecia").text
    assert f'<option value="{oc.id}" selected' in page and "Odnowienie: Ubezpieczenie OC" in page

    ends = today() + timedelta(days=385)
    r = auth_client.post(
        f"/zdjecia/{att.id}/zapisz",
        data={"item_id": car.id, "task_id": str(oc.id), "ends_on": ends.isoformat(), "amount": "640,50"},
        headers=HX,
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    t = db.get(Task, oc.id)
    assert t.next_due_date == ends and t.amount_gr == 64050 and len(t.completions) == 1
    assert db.query(Warranty).count() == 0  # no second reminder next to the insurance
    assert db.query(Transaction).one().amount_gr == -64050
    assert db.get(Attachment, att.id).completion_id == t.completions[0].id


def test_policy_mid_term_is_only_filed(auth_client, db, car, oc):
    oc.next_due_date = today() + timedelta(days=200)
    db.commit()
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, item_id=car.id, kind="policy")
    db.commit()
    assert f'<option value="d{oc.id}" selected' in auth_client.get("/zdjecia").text
    auth_client.post(f"/zdjecia/{att.id}/zapisz", data={"item_id": car.id, "task_id": f"d{oc.id}"}, headers=HX)
    db.expire_all()
    a = db.get(Attachment, att.id)
    assert not a.inbox and a.task_id == oc.id and a.doc_kind == "policy"
    assert not db.get(Task, oc.id).completions and db.query(Warranty).count() == 0


def test_no_next_time(auth_client, db, car):
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, item_id=car.id, kind="service")
    db.commit()
    auth_client.post(f"/zdjecia/{att.id}/zapisz", data={"item_id": car.id, "task_id": "", "next": "none"}, headers=HX)
    t = db.query(Task).one()
    assert not t.active and len(t.completions) == 1


def test_receipt_goes_to_the_budget(auth_client, db, car):
    cat = db.query(BudgetCategory).one()
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, item_id=car.id, kind="receipt")
    db.commit()
    bad = auth_client.post(f"/zdjecia/{att.id}/zapisz", data={"item_id": car.id, "amount": ""}, headers=HX)
    assert bad.status_code == 400 and "kwotę" in bad.text
    auth_client.post(
        f"/zdjecia/{att.id}/zapisz",
        data={"item_id": car.id, "amount": "54,90", "category_id": cat.id, "note": "Płyn do spryskiwaczy"},
        headers=HX,
    )
    tx = db.query(Transaction).one()
    assert (tx.amount_gr, tx.budget_category_id, tx.item_id, tx.note) == (-5490, cat.id, car.id, "Płyn do spryskiwaczy")
    db.expire_all()
    assert not db.get(Attachment, att.id).inbox


def test_policy_with_a_reminder(auth_client, db, car):
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, item_id=car.id, kind="policy")
    db.commit()
    ends = today() + timedelta(days=300)
    auth_client.post(
        f"/zdjecia/{att.id}/zapisz",
        data={"item_id": car.id, "doc_kind": "policy", "name": "OC 2027", "ends_on": ends.isoformat()},
        headers=HX,
    )
    w = db.query(Warranty).one()
    assert w.attachment_id == att.id and w.ends_on == ends and w.task_id
    assert db.get(Task, w.task_id).next_due_date == ends
    db.expire_all()
    assert db.get(Attachment, att.id).doc_kind == "policy"


def test_needs_an_item_and_delete(auth_client, db, car):
    _snap(auth_client)
    att = db.query(Attachment).one()
    inbox.tag(db, att, kind="other")
    db.commit()
    r = auth_client.post(f"/zdjecia/{att.id}/zapisz", data={"note": "x"}, headers=HX)
    assert r.status_code == 400 and "czego to dotyczy" in r.text
    path = documents.file_path(att)
    assert path.exists()
    auth_client.post(f"/zdjecia/{att.id}/usun", headers=HX)
    assert db.query(Attachment).count() == 0 and not path.exists()
    assert auth_client.post(f"/zdjecia/{att.id}/usun", headers=HX).status_code == 404


def test_page_upload_and_list(auth_client, db, car):
    r = auth_client.post(
        "/zdjecia", data={"from": "page"}, files={"files": ("a.jpg", _jpeg(), "image/jpeg")}, follow_redirects=False
    )
    assert r.status_code == 303
    page = auth_client.get("/zdjecia").text
    assert "Do rozpoznania <small" in page and "Wybierz, czego dotyczy" in page
    bad = auth_client.post("/zdjecia", files={"files": ("a.jpg", b"not an image", "image/jpeg")}, headers=HX)
    assert bad.status_code == 400


def test_next_amount_rounding():
    assert inbox.next_amount(85000) == 89000 and inbox.next_amount(14900) == 16000 and inbox.next_amount(100) == 0


def test_migration_0029(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn, conn.begin():
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        cols = {r[1] for r in conn.execute(sa.text('PRAGMA table_info("attachment")'))}
        assert {"inbox", "capture_kind"} <= cols
        command.downgrade(cfg, "9d4b6e1a3f57")
        cols = {r[1] for r in conn.execute(sa.text('PRAGMA table_info("attachment")'))}
        assert "inbox" not in cols
        command.upgrade(cfg, "head")
