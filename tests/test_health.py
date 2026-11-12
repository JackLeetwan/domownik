"""🩺 Zdrowie: health tasks from anywhere land with the right person, issues / medicines / records / links,
🔒 private issues hide everything filed under them."""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from domownik import health
from domownik.config import today
from domownik.models import (
    Attachment,
    ExternalCalendar,
    ExternalEvent,
    HealthLink,
    Issue,
    Item,
    Medication,
    Task,
    User,
)
from domownik.security import hash_password
from tests.conftest import csrf_of


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (0, 120, 0)).save(buf, "PNG")
    return buf.getvalue()


def _task(item_id: int, title: str, days: int = 5, **kw) -> Task:
    d = today() + timedelta(days=days)
    kw.setdefault("category", "health")
    return Task(
        item_id=item_id,
        title=title,
        recurrence={"mode": "once", "date": d.isoformat()},
        next_due_date=d,
        lead_days=2,
        direction="none",
        **kw,
    )


@pytest.fixture
def family(db, user):
    house = Item(name="Dom", kind="house")
    me = Item(name="Jan", kind="member", user_id=user.id)
    kid = Item(name="Ala", kind="child")
    insurance = Item(name="Ubezpieczenia", kind="group")
    db.add_all([house, me, kid, insurance])
    db.commit()
    return {"house": house, "me": me, "kid": kid, "ins": insurance}


def test_menu_and_overview_collect_health_tasks(auth_client, db, user, family):
    me, kid, house, ins = family["me"], family["kid"], family["house"], family["ins"]
    issue = Issue(item_id=me.id, title="Ból kolana", started_on=today())
    db.add(issue)
    db.commit()
    db.add_all(
        [
            _task(me.id, "Ortopeda kontrola"),
            _task(kid.id, "Bilans 2-latka"),
            _task(house.id, "Szczepienie – grypa", assignee_id=user.id),  # assigned → Jan
            _task(house.id, "Apteczka – przegląd"),  # nobody's → "Inne"
            _task(ins.id, "Sprawdzić decyzję ubezpieczyciela", category="insurance", issue_id=issue.id),
            _task(me.id, "Wymiana opon", category="service"),  # not health
        ]
    )
    db.commit()
    page = auth_client.get("/").text
    assert 'href="/zdrowie"' in page
    r = auth_client.get("/zdrowie")
    assert r.status_code == 200
    html = r.text
    for t in ("Ortopeda kontrola", "Bilans 2-latka", "Apteczka – przegląd", "Ból kolana"):
        assert t in html
    assert "Wymiana opon" not in html
    tree_rows = auth_client.get(f"/zdrowie/{me.id}").text
    assert "Szczepienie – grypa" in tree_rows
    assert "Sprawdzić decyzję ubezpieczyciela" in tree_rows  # part of Jan's issue though filed elsewhere
    assert "Bilans 2-latka" not in tree_rows
    assert "Bilans 2-latka" in auth_client.get(f"/zdrowie/{kid.id}").text


def test_calendar_doctor_events_show_up(auth_client, db, family):
    cal = ExternalCalendar(name="Rodzinny", url="https://example.com/c.ics")
    db.add(cal)
    db.commit()
    d = today() + timedelta(days=3)
    db.add_all(
        [
            ExternalEvent(calendar_id=cal.id, starts_on=d, ends_on=d, starts_at="10:00", title="Dentysta – Ala"),
            ExternalEvent(calendar_id=cal.id, starts_on=d, ends_on=d, title="Urodziny babci"),
        ]
    )
    db.commit()
    html = auth_client.get("/zdrowie").text
    assert "Dentysta – Ala" in html
    assert "Urodziny babci" not in html


def test_looks_medical():
    assert health.looks_medical("Wizyta u pediatry")
    assert health.looks_medical("USG brzucha")
    assert health.looks_medical("dr. Kowalska 14:00")
    assert not health.looks_medical("Basen")
    assert not health.looks_medical("Drukarka – toner")


def test_issue_medicine_file_link_flow(auth_client, db, family):
    me = family["me"]
    r = auth_client.post(
        f"/zdrowie/{me.id}/sprawy",
        data={"title": "Skręcona kostka", "started_on": "2026-11-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    issue = db.scalar(select(Issue).where(Issue.title == "Skręcona kostka"))
    assert r.headers["location"] == f"/zdrowie/sprawa/{issue.id}"
    back = f"/zdrowie/sprawa/{issue.id}"
    r = auth_client.post(
        f"/zdrowie/{me.id}/leki",
        data={
            "name": "Ibuprofen",
            "dose": "400 mg",
            "schedule": "3× dziennie",
            "prescription": "kod 1234",
            "issue_id": issue.id,
            "next": back,
            "started_on": today().isoformat(),
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == back
    med = db.scalar(select(Medication))
    assert (med.issue_id, med.prescription, med.ends_on) == (issue.id, "kod 1234", None)
    r = auth_client.post(
        f"/zdrowie/{me.id}/pliki",
        data={"doc_kind": "imaging", "description": "RTG kostki", "issue_id": issue.id},
        files={"files": ("rtg.png", io.BytesIO(_png()), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    att = db.scalar(select(Attachment))
    assert (att.issue_id, att.doc_kind) == (issue.id, "imaging")
    r = auth_client.post(
        f"/zdrowie/{me.id}/linki",
        data={"url": "drive.google.com/drive/folders/abc", "title": "Folder", "issue_id": issue.id},
        follow_redirects=False,
    )
    assert db.scalar(select(HealthLink)).url == "https://drive.google.com/drive/folders/abc"
    assert auth_client.post(f"/zdrowie/{me.id}/linki", data={"url": "javascript:alert(1)"}).status_code == 400
    auth_client.post(f"/zdrowie/sprawa/{issue.id}/zadanie", data={"title": "Kontrola ortopedy", "on": "2026-12-01"})
    page = auth_client.get(back).text
    for t in ("Ibuprofen", "kod 1234", "RTG kostki", "Folder", "Kontrola ortopedy"):
        assert t in page
    person = auth_client.get(f"/zdrowie/{me.id}").text
    assert "Ibuprofen" in person and "RTG kostki" in person and "Kontrola ortopedy" in person
    # stopped: moves to "Wcześniej"
    auth_client.post(f"/zdrowie/leki/{med.id}/koniec")
    db.refresh(med)
    assert med.ends_on == today() - timedelta(days=1) or med.ends_on == med.started_on
    assert not med.current(today())


def test_private_issue_hides_its_things_from_others(client, db, user, family):
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.commit()
    me = family["me"]
    issue = Issue(item_id=me.id, title="Sprawa-tajna", started_on=today(), private_to_id=user.id)
    db.add(issue)
    db.commit()
    db.add_all(
        [
            Medication(item_id=me.id, issue_id=issue.id, name="Lek-tajny", started_on=today()),
            Medication(item_id=me.id, name="Lek-prywatny", started_on=today(), private_to_id=user.id),
            Medication(item_id=me.id, name="Lek-wspólny", started_on=today()),
            HealthLink(item_id=me.id, issue_id=issue.id, title="Link-tajny", url="https://example.com"),
        ]
    )
    db.commit()
    page = client.get("/login")
    client.post("/login", data={"email": "ola@example.com", "password": "tajnehaslo2", "csrf": csrf_of(page.text)})
    html = client.get(f"/zdrowie/{me.id}").text
    assert "Lek-wspólny" in html
    for secret in ("Sprawa-tajna", "Lek-tajny", "Lek-prywatny", "Link-tajny"):
        assert secret not in html
    assert client.get(f"/zdrowie/sprawa/{issue.id}").status_code == 404
    assert "Sprawa-tajna" not in client.get(f"/items/{me.id}").text


def test_private_switch_on_issue_takes_its_tasks(auth_client, db, user, family):
    me = family["me"]
    issue = Issue(item_id=me.id, title="Leczenie zęba", started_on=today())
    db.add(issue)
    db.commit()
    t = _task(me.id, "Dentysta – druga wizyta", issue_id=issue.id)
    db.add(t)
    db.commit()
    auth_client.post(f"/zdrowie/sprawa/{issue.id}/prywatna", data={"private": "1"})
    db.refresh(issue)
    db.refresh(t)
    assert issue.private_to_id == user.id and t.private_to_id == user.id
    auth_client.post(f"/zdrowie/sprawa/{issue.id}/prywatna", data={})
    db.refresh(issue)
    db.refresh(t)
    assert issue.private_to_id is None and t.private_to_id is None


def test_visit_from_health_page_returns_there(auth_client, db, family):
    kid = family["kid"]
    r = auth_client.post(
        f"/items/{kid.id}/visit?next=/zdrowie/{kid.id}",
        data={"title": "Pediatra", "on": (today() + timedelta(days=2)).isoformat()},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/zdrowie/{kid.id}"
    assert db.scalar(select(Task).where(Task.title == "Pediatra")).category == "health"


def test_not_a_member_is_404(auth_client, family):
    assert auth_client.get(f"/zdrowie/{family['house'].id}").status_code == 404


def test_medication_current():
    m = Medication(name="x", started_on=date(2026, 1, 1), ends_on=date(2026, 1, 10))
    assert m.current(date(2026, 1, 5)) and not m.current(date(2026, 1, 11)) and not m.current(date(2025, 12, 31))


# ---------------------------------------------------------------- stage 2


def test_profile_shows_and_goes_to_emergency_folder(auth_client, db, family):
    me = family["me"]
    auth_client.post(f"/zdrowie/{me.id}/profil", data={"blood_type": "A Rh+", "allergies": "penicylina"})
    db.add(Medication(item_id=me.id, name="Lek-stały", dose="1 tabl.", started_on=today()))
    db.add(Medication(item_id=me.id, name="Lek-ukryty", started_on=today(), private_to_id=1))
    db.commit()
    db.refresh(me)
    assert me.attributes["blood_type"] == "A Rh+"
    assert "penicylina" in auth_client.get(f"/zdrowie/{me.id}").text
    folder = auth_client.get("/na-wypadek").text
    assert "Grupa krwi: A Rh+" in folder and "penicylina" in folder and "Lek-stały" in folder
    assert "Lek-ukryty" not in folder
    auth_client.post(f"/zdrowie/{me.id}/profil", data={"blood_type": ""})
    db.refresh(me)
    assert "blood_type" not in me.attributes


def test_medicine_reminder_is_a_supplement_until_it_ends(auth_client, db, user, family):
    from domownik import supplements
    from domownik.models import Supplement

    me = family["me"]
    auth_client.post(
        f"/zdrowie/{me.id}/leki",
        data={"name": "Antybiotyk", "dose": "500 mg", "remind_field": "1", "remind": "1", "remind_time": "8:00"},
    )
    med = db.scalar(select(Medication).where(Medication.name == "Antybiotyk"))
    s = db.scalar(select(Supplement).where(Supplement.medication_id == med.id))
    assert (s.user_id, s.time, s.active, s.name) == (user.id, "08:00", True, "💊 Antybiotyk")
    assert "🔔 08:00" in auth_client.get(f"/zdrowie/{me.id}").text
    # ended → the reminder stops (the minute job)
    med.ends_on = today() - timedelta(days=1)
    db.commit()
    supplements.run(db)
    db.refresh(s)
    assert s.active is False
    # switched off in the form → gone
    med.ends_on = None
    db.commit()
    auth_client.post(f"/zdrowie/leki/{med.id}/edit", data={"name": "Antybiotyk", "remind_field": "1"})
    assert db.scalar(select(Supplement).where(Supplement.medication_id == med.id)) is None


def test_prescription_renewal_task(auth_client, db, family):
    me = family["me"]
    until = today() + timedelta(days=30)
    auth_client.post(f"/zdrowie/{me.id}/leki", data={"name": "Euthyrox", "rx_until": until.isoformat()})
    med = db.scalar(select(Medication).where(Medication.name == "Euthyrox"))
    t = db.get(Task, med.rx_task_id)
    tid = t.id
    assert (t.title, t.next_due_date, t.category, t.lead_days) == ("Recepta: Euthyrox – odnowić", until, "health", 7)
    assert "Recepta: Euthyrox" in auth_client.get(f"/zdrowie/{me.id}").text  # among the visits
    auth_client.post(f"/zdrowie/leki/{med.id}/edit", data={"name": "Euthyrox", "rx_until": ""})
    db.expire_all()
    assert db.get(Task, tid) is None and db.get(Medication, med.id).rx_task_id is None


def test_readings_with_chart(auth_client, db, family):
    me = family["me"]
    for d, v, v2 in ((10, "130", "85"), (5, "125", "82"), (0, "120,5", "80")):
        r = auth_client.post(
            f"/zdrowie/{me.id}/pomiary",
            data={
                "name": "Ciśnienie",
                "value": v,
                "value2": v2,
                "measured_on": (today() - timedelta(days=d)).isoformat(),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
    assert auth_client.post(f"/zdrowie/{me.id}/pomiary", data={"name": "Ciśnienie", "value": "120"}).status_code == 400
    html = auth_client.get(f"/zdrowie/{me.id}").text
    assert "120,5/80 mmHg" in html and "hl-chart" in html and "3 pomiarów" in html


def test_health_spending_by_person(auth_client, db, family):
    from domownik.models import BudgetCategory, Transaction

    me = family["me"]
    cat = BudgetCategory(name="Zdrowie", kind="planned")
    db.add(cat)
    db.commit()
    db.add_all(
        [
            Transaction(booked_on=today(), amount_gr=-25000, budget_category_id=cat.id, item_id=me.id, note="Ortopeda"),
            Transaction(booked_on=today(), amount_gr=-5000, budget_category_id=cat.id, note="Apteka"),
        ]
    )
    db.commit()
    over = auth_client.get("/zdrowie").text
    assert "Wydatki na zdrowie" in over and "300\xa0zł" in over
    person = auth_client.get(f"/zdrowie/{me.id}").text
    assert "Ortopeda" in person and "Apteka" not in person
