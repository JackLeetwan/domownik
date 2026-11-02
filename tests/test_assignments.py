"""Who takes care of what: member ↔ account link, /przypisania, initials, the switch in the task window."""

from datetime import timedelta

from domownik import notify
from domownik.config import today
from domownik.models import Item, PushSubscription, Task, User
from domownik.security import hash_password
from domownik.web import avatar, avatar_class, initials


def _ola(db) -> User:
    u = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo1"))
    db.add(u)
    db.commit()
    return u


def _setup(db):
    zone = Item(name="Dom", kind="house")
    db.add(zone)
    db.flush()
    sub = Item(name="Pralka", kind="appliance", parent_id=zone.id)
    member = Item(name="Ala", kind="member")
    db.add_all([sub, member])
    db.flush()
    t0 = today()
    tasks = [
        Task(
            item_id=sub.id,
            title="Odkamienianie",
            category="service",
            next_due_date=t0 + timedelta(days=3),
            recurrence={"mode": "interval", "every": 1, "unit": "month", "anchor": "due"},
        ),
        Task(
            item_id=zone.id,
            title="Czynsz",
            category="payment",
            next_due_date=t0 + timedelta(days=10),
            recurrence={"mode": "interval", "every": 1, "unit": "month", "anchor": "due"},
        ),
        Task(
            item_id=member.id,
            title="Karta sportowa",
            category="payment",
            next_due_date=t0 + timedelta(days=1),
            recurrence={"mode": "interval", "every": 1, "unit": "month", "anchor": "due"},
        ),
    ]
    db.add_all(tasks)
    db.commit()
    return zone, sub, member, tasks


def test_initials_and_fixed_colour():
    assert initials("Ala") == "A"
    assert initials("Ala Nowak") == "AN"
    assert initials("") == "?"
    assert avatar_class(1) == avatar_class(7) == "av-0"
    assert avatar(None) == ""

    class U:
        id, name = 2, "<Ala>"

    html = avatar(U())
    assert "av-1" in html and "&lt;Ala&gt;" in html and ">&lt;</span>" in html


def test_link_member_to_account_in_edit_form(auth_client, db, user):
    _, _, member, _ = _setup(db)
    page = auth_client.get(f"/items/{member.id}/edit").text
    assert "Konto w Domowniku" in page
    r = auth_client.post(f"/items/{member.id}/edit", data={"name": "Ala", "kind": "member", "user_id": str(user.id)})
    assert r.status_code in (200, 303)
    db.refresh(member)
    assert member.user_id == user.id

    # one account = one person: linking another member item moves the link
    other = Item(name="Ala 2", kind="member")
    db.add(other)
    db.commit()
    auth_client.post(f"/items/{other.id}/edit", data={"name": "Ala 2", "kind": "member", "user_id": str(user.id)})
    db.refresh(member)
    db.refresh(other)
    assert other.user_id == user.id and member.user_id is None

    # unknown account → 400, unchanged
    r = auth_client.post(f"/items/{other.id}/edit", data={"name": "Ala 2", "kind": "member", "user_id": "999"})
    assert r.status_code == 400


def test_non_member_items_have_no_account(auth_client, db, user):
    zone, *_ = _setup(db)
    assert "Konto w Domowniku" not in auth_client.get(f"/items/{zone.id}/edit").text
    auth_client.post(f"/items/{zone.id}/edit", data={"name": "Dom", "kind": "house", "user_id": str(user.id)})
    db.refresh(zone)
    assert zone.user_id is None


def test_person_page_lists_tasks_assigned_house_wide(auth_client, db, user):
    _, _, member, tasks = _setup(db)
    page = auth_client.get(f"/items/{member.id}").text
    assert "Zajmuje się" in page and "Konto w Domowniku" in page  # hint how to link

    member.user_id = user.id
    tasks[0].assignee_id = user.id  # in another zone
    tasks[1].assignee_id = user.id
    db.commit()
    page = auth_client.get(f"/items/{member.id}").text
    care = page.split('id="zajmuje-sie"', 1)[1].split("</section>", 1)[0]
    assert "Odkamienianie" in care and "Czynsz" in care and "Karta sportowa" not in care
    assert "Dom › Pralka" in care  # zone path shown
    assert care.index("Odkamienianie") < care.index("Czynsz")  # by due date
    assert 'class="avatar av-0"' in care


def test_assignments_page_groups_by_zone_and_counts(auth_client, db, user):
    _ola(db)
    _setup(db)
    page = auth_client.get("/przypisania").text
    assert "powiadomienie dostają wszyscy" in page
    assert '<strong id="unassigned-count">3</strong> nieprzypisanych' in page
    assert "Dom" in page and "Ala" in page  # member item is its own zone
    assert "Pralka" in page  # sub-path under the zone
    assert ">J</span> Jan" in page and ">O</span> Ola" in page  # one button per account


def test_assign_one_from_list_returns_row_and_counter(auth_client, db, user):
    _, _, _, tasks = _setup(db)
    r = auth_client.post(f"/przypisania/{tasks[0].id}", data={"assignee_id": str(user.id)})
    assert r.status_code == 200
    assert f'id="arow-{tasks[0].id}"' in r.text
    assert 'id="unassigned-count" hx-swap-oob="true">2<' in r.text
    db.refresh(tasks[0])
    assert tasks[0].assignee_id == user.id

    # back to shared
    r = auth_client.post(f"/przypisania/{tasks[0].id}", data={"assignee_id": ""})
    db.refresh(tasks[0])
    assert tasks[0].assignee_id is None


def test_assign_rejects_unknown_or_inactive_account(auth_client, db, user):
    _, _, _, tasks = _setup(db)
    ola = _ola(db)
    ola.is_active = False
    db.commit()
    assert auth_client.post(f"/przypisania/{tasks[0].id}", data={"assignee_id": "999"}).status_code == 400
    assert auth_client.post(f"/przypisania/{tasks[0].id}", data={"assignee_id": str(ola.id)}).status_code == 400
    assert auth_client.post(f"/przypisania/{tasks[0].id}", data={"assignee_id": "x"}).status_code == 400
    assert auth_client.post("/przypisania/99999", data={"assignee_id": ""}).status_code == 404


def test_assign_requires_csrf(auth_client, db, user):
    _, _, _, tasks = _setup(db)
    auth_client.headers.pop("X-CSRF-Token")
    r = auth_client.post(f"/przypisania/{tasks[0].id}", data={"assignee_id": str(user.id)})
    assert r.status_code in (400, 403)
    db.refresh(tasks[0])
    assert tasks[0].assignee_id is None


def test_bulk_assign_and_filter(auth_client, db, user):
    ola = _ola(db)
    _, _, _, tasks = _setup(db)
    r = auth_client.post(
        "/przypisania/zbiorczo",
        data={"task_id": [str(tasks[0].id), str(tasks[1].id)], "assignee_id": str(ola.id), "tylko": "nieprzypisane"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/przypisania?tylko=nieprzypisane"
    for t in tasks:
        db.refresh(t)
    assert [t.assignee_id for t in tasks] == [ola.id, ola.id, None]

    page = auth_client.get("/przypisania?tylko=nieprzypisane").text
    assert "Karta sportowa" in page and "Odkamienianie" not in page

    auth_client.post("/przypisania/zbiorczo", data={"task_id": [str(tasks[0].id)], "assignee_id": ""})
    db.refresh(tasks[0])
    assert tasks[0].assignee_id is None


def test_initials_on_dashboard_rows_and_switch_in_task_window(auth_client, db, user):
    _, _, _, tasks = _setup(db)
    page = auth_client.get("/").text
    assert 'class="avatar' not in page.split("<main", 1)[1]

    tasks[0].assignee_id = user.id
    db.commit()
    page = auth_client.get("/").text
    assert f'data-task="{tasks[0].id}"><span class="avatar av-0"' in page

    modal = auth_client.get(f"/tasks/{tasks[1].id}").text
    assert 'id="assign-' in modal and "Kto:" in modal and 'aria-pressed="true"' in modal
    r = auth_client.post(f"/przypisania/{tasks[1].id}?ctx=modal", data={"assignee_id": str(user.id)})
    assert f'id="assign-{tasks[1].id}"' in r.text
    assert f"hx-swap-oob=\"outerHTML:.who-slot[data-task='{tasks[1].id}']\"" in r.text
    assert 'class="avatar av-0"' in r.text


def test_payment_group_shows_assignees(db, user):
    from domownik import services
    from domownik.domain.tree import Tree

    zone = Item(name="Dom", kind="house")
    db.add(zone)
    db.flush()
    d = today() + timedelta(days=4)
    for i in range(3):
        db.add(
            Task(
                item_id=zone.id,
                title=f"Opłata {i}",
                category="payment",
                next_due_date=d,
                assignee_id=user.id if i else None,
                recurrence={"mode": "once"},
            )
        )
    db.commit()
    rows = services.task_rows(db, Tree.load(db), today())
    (g,) = services.group_payments(rows)
    assert [u.id for u in g.assignees] == [user.id]


def test_my_filter_hint_only_when_nothing_assigned(auth_client, db, user):
    _, _, _, tasks = _setup(db)
    assert "/przypisania" in auth_client.get("/?who=me").text.split('class="agenda"', 1)[1].split("</section>")[0]
    assert "Żadne zadanie nie jest jeszcze przypisane" not in auth_client.get("/").text
    tasks[0].assignee_id = user.id
    db.commit()
    assert "Żadne zadanie nie jest jeszcze przypisane" not in auth_client.get("/?who=me").text
    assert "/przypisania" in auth_client.get("/ustawienia").text


def test_assignment_from_list_drives_notifications(auth_client, db, user):
    """Assigned → only that person; shared → everybody (notify.py rule, set through the new screen)."""
    ola = _ola(db)
    for u, ep in ((user, "https://push.example.com/a"), (ola, "https://push.example.com/b")):
        db.add(PushSubscription(user_id=u.id, endpoint=ep, p256dh="pk", auth="au"))
    zone = Item(name="Dom", kind="house")
    db.add(zone)
    db.flush()
    t0 = today()
    mine = Task(item_id=zone.id, title="Przegląd", category="service", next_due_date=t0, recurrence={"mode": "once"})
    shared = Task(item_id=zone.id, title="Śmieci", category="other", next_due_date=t0, recurrence={"mode": "once"})
    db.add_all([mine, shared])
    db.commit()
    auth_client.post(f"/przypisania/{mine.id}", data={"assignee_id": str(ola.id)})
    db.expire_all()

    by_name = {d.user.name: d.message() for d in notify.build_digests(db, "morning", t0)}
    assert "Przegląd" in by_name["Ola"] and "Przegląd" not in by_name["Jan"]
    assert "Śmieci" in by_name["Ola"] and "Śmieci" in by_name["Jan"]


def test_migration_links_member_only_on_unique_name(tmp_path):
    """0008: item.user_id is filled only when exactly one member and one account share the name."""
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")  # foreign_keys=ON, like the app
    cfg = cli.alembic_config()
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "4b0b465923fa")
        conn.execute(
            sa.text(
                "INSERT INTO user (id, name, email, password_hash, is_active, notify_morning, notify_evening) VALUES "
                "(1, 'Ala', 'a@x', 'h', 1, 1, 1), (2, 'Jan', 'j@x', 'h', 1, 1, 1), (3, 'Ola', 'o@x', 'h', 1, 1, 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO item (id, kind, name, sort_order, attributes, archived) VALUES "
                "(1, 'member', 'Ala', 0, '{}', 0), (2, 'member', 'Jan', 0, '{}', 0), (3, 'member', 'Jan', 0, '{}', 0),"
                "(4, 'child', 'Ola', 0, '{}', 0), (5, 'member', 'Ewa', 0, '{}', 0)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO item (id, parent_id, kind, name, sort_order, attributes, archived) "
                "VALUES (6, 1, 'other', 'Rower', 0, '{}', 0)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO task (item_id, title, category, recurrence, lead_days, direction, autopay, active) "
                "VALUES (6, 'Smarowanie', 'service', '{}', 7, 'none', 0, 1)"
            )
        )

        def counts():
            return [conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar() for t in ("item", "task")]

        command.upgrade(cfg, "head")
        assert counts() == [6, 1]  # adding the column must not rebuild (and cascade-empty) the table
        links = dict(conn.execute(sa.text("SELECT id, user_id FROM item ORDER BY id")).fetchall())
        assert links == {1: 1, 2: None, 3: None, 4: None, 5: None, 6: None}
    with eng.connect() as conn:  # downgrade outside a transaction: it rebuilds 'item' with foreign keys off
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "4b0b465923fa")
        conn.commit()
        assert [conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar() for t in ("item", "task")] == [6, 1]
    eng.dispose()
