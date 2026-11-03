"""Contacts (/kontakty): CRUD, links to several items, search, vendor suggestions."""

from __future__ import annotations

from sqlalchemy import select

from domownik.models import Contact, Item, Task, contact_item
from domownik.routers.contacts import clean_website, grouped, map_href, tel_href, vendor_suggestions
from domownik.search import search

HX = {"HX-Request": "true"}


def _cars(db):
    a, b = Item(name="Auto 1", kind="car"), Item(name="Auto 2", kind="car")
    db.add_all([a, b])
    db.commit()
    return a, b


def test_create_with_two_items_and_list(auth_client, db):
    a, b = _cars(db)
    r = auth_client.post(
        "/kontakty/nowy",
        data={
            "name": "Pan Marek",
            "company": "Serwis X",
            "role": "Mechanik / serwis auta",
            "phones": "+48 600 100 200\n22 111 22 33",
            "email": "serwis@example.com",
            "website": "serwis.example.com",
            "address": "ul. Prosta 1, Miasto",
            "item_id": [str(a.id), str(b.id)],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    c = db.scalar(select(Contact))
    assert c.phone_list == ["+48 600 100 200", "22 111 22 33"]
    assert c.website == "https://serwis.example.com"
    assert {i.id for i in c.items} == {a.id, b.id}
    page = auth_client.get("/kontakty").text
    assert "Pan Marek" in page and "tel:+48600100200" in page and "mailto:serwis@example.com" in page
    assert "google.com/maps/search/?api=1&amp;query=ul.%20Prosta%201%2C%20Miasto" in page
    # both car pages show it
    assert "Pan Marek" in auth_client.get(f"/items/{a.id}").text
    assert "Pan Marek" in auth_client.get(f"/items/{b.id}").text


def test_edit_unlink_link_delete(auth_client, db):
    a, b = _cars(db)
    c = Contact(name="Hydraulik Jan", role="Hydraulik", items=[a])
    db.add(c)
    db.commit()
    r = auth_client.post(
        f"/kontakty/{c.id}/edit", data={"name": "Jan", "company": "", "item_id": [str(b.id)]}, follow_redirects=False
    )
    assert r.status_code == 303
    db.expire_all()
    assert c.name == "Jan" and c.role is None and [i.id for i in c.items] == [b.id]

    auth_client.post(f"/items/{a.id}/kontakty", data={"contact_id": c.id})
    db.expire_all()
    assert {i.id for i in c.items} == {a.id, b.id}
    auth_client.post(f"/items/{a.id}/kontakty", data={"contact_id": c.id})  # twice → still once
    assert db.execute(select(contact_item)).all().__len__() == 2

    auth_client.post(f"/items/{b.id}/kontakty/{c.id}/odepnij")
    db.expire_all()
    assert [i.id for i in c.items] == [a.id]

    auth_client.post(f"/kontakty/{c.id}/delete")
    db.expire_all()
    assert db.scalar(select(Contact)) is None
    assert db.execute(select(contact_item)).all() == []
    assert db.get(Item, a.id) is not None  # the item stays


def test_deleting_item_keeps_contact(db):
    a, b = _cars(db)
    c = Contact(name="Mechanik", items=[a, b])
    db.add(c)
    db.commit()
    db.delete(a)
    db.commit()
    db.expire_all()
    assert [i.id for i in db.get(Contact, c.id).items] == [b.id]


def test_name_or_company_required(auth_client):
    r = auth_client.post("/kontakty/nowy", data={"name": " ", "company": ""}, headers=HX)
    assert r.status_code == 400


def test_list_search_and_grouping(auth_client, db):
    a, _ = _cars(db)
    db.add_all(
        [
            Contact(name="Przedszkole Słoneczko", role="Przedszkole / szkoła", phones="111"),
            Contact(name="Dr Nowak", role="Lekarz / przychodnia"),
            Contact(name="Ktoś", role="Kominiarz"),
            Contact(name="Bez roli", items=[a]),
        ]
    )
    db.commit()
    groups = grouped(list(db.scalars(select(Contact))))
    assert [g[0] for g in groups] == ["Przedszkole / szkoła", "Lekarz / przychodnia", "Kominiarz", "Inne"]
    page = auth_client.get("/kontakty?q=sloneczko").text  # no diacritics needed
    assert "Przedszkole Słoneczko" in page and "Dr Nowak" not in page
    page = auth_client.get("/kontakty?q=auto 1").text  # the linked item counts too
    assert "Bez roli" in page and "Dr Nowak" not in page
    page = auth_client.get(f"/kontakty?item={a.id}").text
    assert "Bez roli" in page and "Dr Nowak" not in page


def test_global_search_finds_contacts(db):
    db.add(Contact(name="Agent", company="Ubezpieczenia Y", phones="500 600 700", role="Ubezpieczyciel / agent"))
    db.commit()
    res = search(db, "ubezpieczenia")
    assert [h.url for h in res.groups["contact"]] == [f"/kontakty#c{db.scalar(select(Contact.id))}"]
    assert search(db, "600 700").groups.get("contact")


def test_vendor_suggestions_in_done_form(auth_client, db):
    a, _ = _cars(db)
    db.add_all([Contact(name="Inny", company="Firma"), Contact(name="Mechanik", items=[a])])
    task = Task(item_id=a.id, title="Przegląd", next_due_date=None)
    db.add(task)
    db.commit()
    assert vendor_suggestions(db, a.id) == ["Mechanik", "Inny (Firma)"]
    r = auth_client.get(f"/tasks/{task.id}", headers=HX)
    assert f'list="vendor-list-{task.id}"' in r.text and '<option value="Mechanik">' in r.text


def test_helpers():
    assert tel_href("+48 (22) 111-22-33") == "tel:+48221112233"
    assert map_href("Rynek 1") == "https://www.google.com/maps/search/?api=1&query=Rynek%201"
    assert clean_website("javascript:alert(1)") is None or clean_website("javascript:alert(1)").startswith("https://")
    assert clean_website("http://x.pl/a") == "http://x.pl/a"
    assert clean_website("bad url") is None
    assert clean_website("") is None


def test_menu_has_contacts(auth_client):
    assert 'href="/kontakty"' in auth_client.get("/").text


def test_migration_0012_up_and_down(tmp_path):
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
            command.upgrade(cfg, "3deccfd9be0c")
            conn.execute(
                sa.text(
                    "INSERT INTO item (id, kind, name, sort_order, attributes, archived) VALUES (1, 'car', 'A', 0, '{}', 0)"
                )
            )
            command.upgrade(cfg, "7c1e5a9d2b40")
            conn.execute(sa.text("INSERT INTO contact (id, name) VALUES (1, 'Mechanik')"))
            conn.execute(sa.text("INSERT INTO contact_item (contact_id, item_id) VALUES (1, 1)"))
            command.downgrade(cfg, "3deccfd9be0c")
            tables = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
            assert not tables & {"contact", "contact_item"}
            assert conn.execute(sa.text("SELECT count(*) FROM item")).scalar() == 1
            command.upgrade(cfg, "head")
