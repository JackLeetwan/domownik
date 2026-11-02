from sqlalchemy import select

from domownik.config import today
from domownik.models import ShoppingItem, Transaction

HX = {"HX-Request": "true", "HX-Target": "shopping-list"}


def _items(db):
    db.expire_all()
    return {s.name: s for s in db.scalars(select(ShoppingItem))}


def test_add_parses_dedups_and_sections(auth_client, db):
    r = auth_client.post("/zakupy", data={"text": "2 mleko, chleb, papier ścierny 2000"}, headers=HX)
    assert r.status_code == 200 and 'id="shopping-list"' in r.text
    items = _items(db)
    assert items["Mleko"].qty == "2" and items["Mleko"].section == "nabial"
    assert items["Chleb"].section == "pieczywo" and items["Papier ścierny 2000"].section == "budowlane"
    # adding again does not duplicate, updates the quantity
    auth_client.post("/zakupy", data={"text": "mleko x3"}, headers=HX)
    items = _items(db)
    assert len(items) == 3 and items["Mleko"].qty == "×3"


def test_dashboard_card_and_redirect(auth_client, db):
    auth_client.post("/setup/zones")
    r = auth_client.post("/zakupy?next=/", data={"text": "masło"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = auth_client.get("/").text
    assert "🛒 Zakupy" in page and "Masło" in page
    r = auth_client.post("/zakupy?next=//evil.example", data={"text": "x"}, follow_redirects=False)
    assert r.headers["location"] == "/zakupy"


def test_cart_finish_and_budget(auth_client, db):
    auth_client.post("/zakupy", data={"text": "mleko, chleb, ser"}, headers=HX)
    items = _items(db)
    auth_client.post(f"/zakupy/{items['Mleko'].id}/check", headers=HX)
    auth_client.post(f"/zakupy/{items['Chleb'].id}/check", headers=HX)
    page = auth_client.get("/zakupy").text
    assert "W koszyku" in page and "Zakończ zakupy" in page
    auth_client.post("/zakupy/zakoncz", data={"amount": "45,90", "shop": "Biedronka"})
    items = _items(db)
    assert items["Mleko"].bought_on == today() and items["Chleb"].bought_on == today()
    assert items["Ser"].bought_on is None
    tr = db.scalar(select(Transaction))
    assert tr.amount_gr == -4590 and "Biedronka" in tr.note and tr.budget_category_id
    # bought items stay as suggestions and keep their section next time
    page = auth_client.get("/zakupy").text
    assert '<option value="Mleko">' in page
    auth_client.post("/zakupy", data={"text": "mleko"}, headers=HX)
    assert len([s for s in db.scalars(select(ShoppingItem)) if s.name == "Mleko"]) == 2


def test_section_memory_edit_delete(auth_client, db):
    auth_client.post("/zakupy", data={"text": "serwetki"}, headers=HX)
    s = _items(db)["Serwetki"]
    assert s.section == "inne"
    auth_client.post(
        f"/zakupy/{s.id}/edit",
        data={"name": "Serwetki", "qty": "1 op.", "section": "chemia", "note": "białe"},
        headers=HX,
    )
    s = _items(db)["Serwetki"]
    assert (s.qty, s.section, s.note) == ("1 op.", "chemia", "białe")
    auth_client.post(f"/zakupy/{s.id}/check", headers=HX)
    auth_client.post("/zakupy/zakoncz", data={})
    assert db.scalar(select(Transaction)) is None  # no amount → no transaction
    auth_client.post("/zakupy", data={"text": "serwetki"}, headers=HX)
    db.expire_all()
    new = next(x for x in db.scalars(select(ShoppingItem)) if x.bought_on is None)
    assert new.id != s.id
    assert new.section == "chemia"  # remembered
    auth_client.post(f"/zakupy/{new.id}/delete", headers=HX)
    db.expire_all()
    assert [x for x in db.scalars(select(ShoppingItem)) if x.bought_on is None] == []


def test_list_fragment(auth_client):
    r = auth_client.get("/zakupy/lista")
    assert r.status_code == 200 and "Lista jest pusta" in r.text and "<html" not in r.text
