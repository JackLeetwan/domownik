from datetime import timedelta

from sqlalchemy import select

from domownik.config import today
from domownik.models import Issue, Item, Task


def _member(auth_client, db, name="Adam"):
    r = auth_client.post(
        "/items/new/member",
        data={"name": name, "attr_blood_type": "A Rh+", "pick_0": "on", "title_0": "Dentysta – kontrola"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return db.scalar(select(Item).where(Item.name == name))


def test_member_on_dashboard_not_as_zone(auth_client, db):
    auth_client.post("/setup/zones")
    adam = _member(auth_client, db)
    assert adam.kind == "member" and adam.parent_id is None
    assert {t.title for t in adam.tasks} == {"Dentysta – kontrola"}
    page = auth_client.get("/").text
    main = page.split("<main", 1)[1]  # skip the sidebar, which lists members and zones too
    members, zones = main.split('<h2 class="section-title">Strefy</h2>', 1)
    assert "Domownicy" in members and "Adam" in members
    assert "Adam" not in zones.split('<section class="agenda">')[0]


def test_child_is_member_too(auth_client, db):
    auth_client.post("/setup/zones")
    kids = db.scalar(select(Item).where(Item.name == "Dzieci"))
    auth_client.post("/items/new/child", data={"parent_id": kids.id, "name": "Staś"})
    page = auth_client.get("/").text.split("<main", 1)[1]
    assert "Staś" in page.split('<h2 class="section-title">Strefy</h2>', 1)[0]
    stas = db.scalar(select(Item).where(Item.name == "Staś"))
    assert "Wizyty i badania" in auth_client.get(f"/items/{stas.id}").text


def test_visit_lands_in_health_section(auth_client, db):
    adam = _member(auth_client, db)
    on = today() + timedelta(days=5)
    r = auth_client.post(
        f"/items/{adam.id}/visit",
        data={"title": "Ortopeda", "on": on.isoformat(), "category": "health", "notes": "skierowanie"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    t = db.scalar(select(Task).where(Task.title == "Ortopeda"))
    assert t.recurrence == {"mode": "once", "date": on.isoformat()} and t.next_due_date == on
    assert t.direction == "none" and t.amount_gr is None
    page = auth_client.get(f"/items/{adam.id}").text
    health, rest = page.split("<h2>Zadania</h2>", 1)
    assert "Ortopeda" in health and "Dentysta" in health
    assert "Ortopeda" not in rest


def test_visit_only_for_members(auth_client, db):
    auth_client.post("/setup/zones")
    dom = db.scalar(select(Item).where(Item.name == "Dom"))
    r = auth_client.post(f"/items/{dom.id}/visit", data={"title": "x", "on": today().isoformat()})
    assert r.status_code == 400
    assert "Wizyty i badania" not in auth_client.get(f"/items/{dom.id}").text


def test_issue_lifecycle(auth_client, db):
    adam = _member(auth_client, db)
    auth_client.post(f"/items/{adam.id}/issues", data={"title": "Ból kolana", "notes": "po bieganiu"})
    issue = db.scalar(select(Issue))
    assert issue.started_on == today() and issue.resolved_on is None
    assert "1 sprawa" in auth_client.get("/").text

    auth_client.post(
        f"/issues/{issue.id}/edit",
        data={"title": "Ból prawego kolana", "notes": "USG zlecone", "started_on": "2026-10-01"},
    )
    db.refresh(issue)
    assert (
        issue.title == "Ból prawego kolana" and issue.notes == "USG zlecone" and str(issue.started_on) == "2026-10-01"
    )

    auth_client.post(f"/issues/{issue.id}/toggle")
    db.refresh(issue)
    assert issue.resolved_on == today()
    assert (
        "sprawa"
        not in auth_client.get("/").text.split("<main", 1)[1].split('<h2 class="section-title">Strefy</h2>', 1)[0]
    )
    page = auth_client.get(f"/items/{adam.id}").text
    assert "Zamknięte (1)" in page

    auth_client.post(f"/issues/{issue.id}/toggle")
    db.refresh(issue)
    assert issue.resolved_on is None

    auth_client.post(f"/issues/{issue.id}/delete")
    db.expire_all()
    assert db.scalar(select(Issue)) is None
