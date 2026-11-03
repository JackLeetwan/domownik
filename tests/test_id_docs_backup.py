"""Personal documents on a member's page (expiry reminder, next date after renewal) and the stale-backup line."""

from __future__ import annotations

import os
import time
from datetime import date, timedelta

import pytest

from domownik import services
from domownik.config import get_settings, today
from domownik.domain import id_docs
from domownik.models import Item, Task


def test_validity_and_recurrence():
    kid = date(2021, 6, 9)
    passport = id_docs.BY_KEY["passport"]
    assert passport.validity_years(kid, date(2027, 1, 1)) == 5
    assert passport.validity_years(kid, date(2035, 1, 1)) == 10
    assert id_docs.BY_KEY["id_card"].validity_years(None, date(2027, 1, 1)) == 10
    rule = id_docs.recurrence_for(id_docs.BY_KEY["driving"], None, date(2030, 5, 1))
    assert rule == {"mode": "interval", "every": 15, "unit": "year", "anchor": "completion"}
    assert id_docs.recurrence_for(id_docs.BY_KEY["ehic"], None, date(2030, 5, 1))["mode"] == "once"


def test_add_and_update_document(auth_client, db):
    person = Item(name="Ola", kind="member")
    house = Item(name="Dom", kind="house")
    db.add_all([person, house])
    db.commit()
    page = auth_client.get(f"/items/{person.id}").text
    assert "🪪 Dokumenty" in page and "Paszport" in page
    assert "🪪 Dokumenty" not in auth_client.get(f"/items/{house.id}").text

    until = today() + timedelta(days=400)
    auth_client.post(f"/items/{person.id}/dokumenty", data={"doc": "passport", "until": until.isoformat()})
    auth_client.post(
        f"/items/{person.id}/dokumenty", data={"doc": "passport", "until": (until + timedelta(days=1)).isoformat()}
    )
    (task,) = db.query(Task).filter_by(item_id=person.id).all()
    assert task.title == "Paszport – ważność" and task.category == "document" and task.lead_days == 90
    assert task.next_due_date == until + timedelta(days=1) and task.recurrence["every"] == 10
    assert (
        auth_client.post(f"/items/{house.id}/dokumenty", data={"doc": "passport", "until": "2030-01-01"}).status_code
        == 400
    )
    assert auth_client.post(f"/items/{person.id}/dokumenty", data={"doc": "x", "until": "nope"}).status_code == 400
    assert "Paszport – ważność" in auth_client.get(f"/items/{person.id}").text


@pytest.fixture
def backups(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")
    auto = get_settings().backups_dir / "auto"
    auto.mkdir(parents=True)
    return auto


def test_backup_warning(backups):
    assert "Nie ma jeszcze żadnej" in services.backup_warning(today())
    f = backups / "domownik-20260101-033000.tar.gz"
    f.write_bytes(b"x")
    assert services.backup_warning(today()) is None
    old = time.time() - 5 * 86400
    os.utime(f, (old, old))
    assert "5 dni temu" in services.backup_warning(today())


def test_child_document_is_one_off():
    kid = date(2021, 6, 9)
    rule = id_docs.recurrence_for(id_docs.BY_KEY["passport"], kid, date(2027, 1, 1))
    assert rule["mode"] == "once"  # the next one (after 13) is valid 10 years – asked again after renewal
    assert id_docs.recurrence_for(id_docs.BY_KEY["passport"], kid, date(2036, 1, 1))["every"] == 10
