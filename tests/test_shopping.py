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


def test_finish_with_wrong_amount_is_a_message_not_a_500(auth_client, db):
    auth_client.post("/zakupy", data={"text": "mleko"}, headers=HX)
    auth_client.post(f"/zakupy/{_items(db)['Mleko'].id}/check", headers=HX)
    for bad in ("test", "nan", "1e999", "12,3,4"):
        r = auth_client.post("/zakupy/zakoncz", data={"amount": bad, "shop": "x"}, headers={"HX-Request": "true"})
        assert r.status_code == 400 and "Nieprawidłowa kwota" in r.text and r.headers["HX-Reswap"] == "none"
    # nothing happened: the cart is still there, no transaction
    assert _items(db)["Mleko"].bought_on is None
    assert db.scalar(select(Transaction)) is None
    # without htmx: an error page, not a crash
    r = auth_client.post("/zakupy/zakoncz", data={"amount": "test"})
    assert r.status_code == 400 and "Nieprawidłowa kwota" in r.text
    # the amount fields tell the browser what a valid amount is
    assert 'pattern="[0-9 ]*([.,][0-9]{1,2})?"' in auth_client.get("/zakupy").text


def test_remove_from_list_with_undo(auth_client, db):
    auth_client.post("/zakupy", data={"text": "2 jajka"}, headers=HX)
    s = _items(db)["Jajka"]
    sid, section = s.id, s.section
    page = auth_client.get("/zakupy").text
    assert f'hx-post="/zakupy/{sid}/delete"' in page and 'class="shop-del"' in page
    r = auth_client.post(f"/zakupy/{sid}/delete", headers=HX)
    assert r.status_code == 200 and "Usunięto „Jajka”" in r.text and 'hx-post="/zakupy/przywroc"' in r.text
    assert "Jajka" not in _items(db)
    # undo brings it back with quantity and section
    r = auth_client.post(
        "/zakupy/przywroc",
        data={"name": "Jajka", "qty": "2", "section": section, "item_id": "", "added_by_id": ""},
        headers=HX,
    )
    back = _items(db)["Jajka"]
    assert back.qty == "2" and back.section == section and back.bought_on is None
    # undo twice does not duplicate
    auth_client.post("/zakupy/przywroc", data={"name": "Jajka"}, headers=HX)
    assert len([x for x in db.scalars(select(ShoppingItem)) if x.name == "Jajka"]) == 1


def test_check_to_is_idempotent(auth_client, db):
    """Taps made offline are replayed with ?to= – sending the same state twice must not toggle back."""
    from domownik.models import ShoppingItem

    s = ShoppingItem(name="Mleko", section="nabial")
    db.add(s)
    db.commit()
    for _ in range(2):
        auth_client.post(f"/zakupy/{s.id}/check?to=1")
        db.refresh(s)
        assert s.checked_at is not None
    auth_client.post(f"/zakupy/{s.id}/check?to=0")
    db.refresh(s)
    assert s.checked_at is None
    auth_client.post(f"/zakupy/{s.id}/check")  # plain toggle still works
    db.refresh(s)
    assert s.checked_at is not None
    assert f"/zakupy/{s.id}/check?to=0" in auth_client.get("/zakupy").text


def test_polish_first_letter_dedups(auth_client, db):
    """SQLite's lower() is ASCII-only – 'Śmietana' and 'śmietana' must still be one row."""
    auth_client.post("/zakupy", data={"text": "śmietana"}, headers={"HX-Request": "true"})
    auth_client.post("/zakupy", data={"text": "Śmietana"}, headers={"HX-Request": "true"})
    assert len(db.scalars(select(ShoppingItem)).all()) == 1
