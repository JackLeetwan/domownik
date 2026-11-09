"""Kitchen: meal plan, recipes → shopping list (pantry-aware), pantry with dates, morning line, finish→pantry."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from domownik import kitchen, notify
from domownik.config import today
from domownik.models import Item, MealPlan, PantryItem, PushSubscription, Recipe, ShoppingItem

HX = {"HX-Request": "true"}


def _recipe(db, name="Naleśniki", ingredients="2 jajka\n500 ml mleka\n250 g mąki\nszczypta soli") -> Recipe:
    r = Recipe(name=name, ingredients=ingredients, tags="słodkie, szybkie")
    db.add(r)
    db.commit()
    return r


def test_pages_render_and_nav(auth_client, db):
    assert auth_client.get("/kuchnia", follow_redirects=False).headers["location"] == "/kuchnia/jadlospis"
    for url in ("/kuchnia/jadlospis", "/kuchnia/przepisy", "/kuchnia/przepisy/nowy", "/kuchnia/spizarnia"):
        r = auth_client.get(url)
        assert r.status_code == 200, url
        assert 'href="/kuchnia"' in r.text  # sidebar link


def test_recipe_crud_and_search(auth_client, db):
    r = auth_client.post(
        "/kuchnia/przepisy",
        data={
            "name": "Spaghetti",
            "ingredients": "500 g makaronu\n- 1 słoik passaty",
            "tags": "obiad,  szybkie",
            "link": "example.com/x",
            "minutes": "30",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    rec = db.scalar(select(Recipe))
    assert rec.ingredient_list == ["500 g makaronu", "1 słoik passaty"]
    assert rec.tags == "obiad, szybkie" and rec.link == "https://example.com/x"
    page = auth_client.get(f"/kuchnia/przepisy/{rec.id}").text
    assert "500 g makaronu" in page and "Dodaj składniki do zakupów" in page
    assert "Spaghetti" in auth_client.get("/kuchnia/przepisy?tag=obiad").text
    assert "Spaghetti" in auth_client.get("/szukaj?q=passaty").text
    r = auth_client.post("/kuchnia/przepisy", data={"name": " "}, headers=HX)
    assert r.status_code == 400


def test_pantry_matching_and_shopping_modal(auth_client, db):
    rec = _recipe(db)
    db.add(PantryItem(name="Mąka pszenna", place="pantry"))
    db.add(PantryItem(name="Jajko", place="fridge"))
    db.commit()
    needs = {n.name: n for n in kitchen.needs([rec], kitchen.pantry_items(db))}
    assert needs["Jajka"].have is not None and needs["Mąka"].have is not None
    assert needs["Mleko"].have is None

    modal = auth_client.get(f"/kuchnia/przepisy/{rec.id}/zakupy", headers=HX).text
    assert 'value="500 ml mleka" checked' in modal and 'value="2 jajka" >' in modal.replace("  ", " ")

    r = auth_client.post("/kuchnia/do-zakupow", data={"entry": ["500 ml mleka", "szczypta soli"]}, headers=HX)
    assert (
        "dm-added" in r.headers["HX-Trigger"]
        and "Dopisano do zakupów: 2" in json.loads(r.headers["HX-Trigger"])["dm-added"]
    )
    names = {s.name: s.qty for s in db.scalars(select(ShoppingItem))}
    assert names["Mleko"] == "500 ml"
    r = auth_client.post("/kuchnia/do-zakupow", data={"entry": ["500 ml mleka"]}, headers=HX)
    assert "ilość dodana: 1" in json.loads(r.headers["HX-Trigger"])["dm-added"]
    db.expire_all()
    assert db.scalar(select(ShoppingItem).where(ShoppingItem.name == "Mleko")).qty == "1000 ml"
    assert auth_client.post("/kuchnia/do-zakupow", data={}, headers=HX).status_code == 400


def test_meal_plan_week_and_week_shopping(auth_client, db):
    rec = _recipe(db)
    monday = kitchen.week_start(today())
    auth_client.post("/kuchnia/jadlospis", data={"day": monday.isoformat(), "meal": "lunch", "what": "naleśniki"})
    auth_client.post(
        "/kuchnia/jadlospis", data={"day": monday.isoformat(), "meal": "dinner", "what": "Pizza na mieście"}
    )
    a, b = db.scalars(select(MealPlan).order_by(MealPlan.id)).all()
    assert a.recipe_id == rec.id and a.text is None  # matched by name
    assert b.recipe_id is None and b.text == "Pizza na mieście"
    page = auth_client.get(f"/kuchnia/jadlospis?t={monday.isoformat()}").text
    assert "Pizza na mieście" in page and "Składniki z tego tygodnia" in page
    modal = auth_client.get(f"/kuchnia/jadlospis/zakupy?t={monday.isoformat()}", headers=HX).text
    assert "250 g mąki" in modal

    # deleting the recipe keeps its name in the plan
    auth_client.post(f"/kuchnia/przepisy/{rec.id}/usun")
    db.expire_all()
    assert db.get(MealPlan, a.id).text == "Naleśniki"
    bid = b.id
    auth_client.post(f"/kuchnia/jadlospis/{bid}/usun")
    db.expire_all()
    assert db.scalar(select(MealPlan).where(MealPlan.id == bid)) is None


def test_dashboard_shows_today_meals(auth_client, db):
    db.add(Item(name="Dom", kind="house"))
    db.add(MealPlan(day=today(), meal="lunch", text="Zupa pomidorowa"))
    db.commit()
    assert "obiad – Zupa pomidorowa" in auth_client.get("/").text


def test_pantry_add_rebuy_used_and_status(auth_client, db):
    soon = today() + timedelta(days=1)
    auth_client.post(
        "/kuchnia/spizarnia", data={"text": "mleko, 2 jogurty", "place": "fridge", "expires_on": soon.isoformat()}
    )
    items = {p.name: p for p in db.scalars(select(PantryItem))}
    assert set(items) == {"Mleko", "Jogurt"} and items["Jogurt"].qty == "2"
    assert kitchen.pantry_status(items["Mleko"], today()) == "soon"
    page = auth_client.get("/kuchnia/spizarnia").text
    assert "Lodówka" in page and "kończy się termin" in page

    auth_client.post(f"/kuchnia/spizarnia/{items['Mleko'].id}/kup")
    assert db.scalar(select(ShoppingItem.name)) == "Mleko"
    auth_client.post(f"/kuchnia/spizarnia/{items['Jogurt'].id}/usun")
    db.expire_all()
    assert db.scalar(select(PantryItem)) is None
    assert auth_client.post("/kuchnia/spizarnia", data={"text": " "}, headers=HX).status_code == 400


def test_morning_push_line(db, user, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "domownik.webpush.deliver", lambda sub, data, urgent=False: sent.append(json.loads(data)) or True
    )
    db.add(PushSubscription(user_id=user.id, endpoint="https://p.example/1", p256dh="k", auth="a"))
    db.add(PantryItem(name="Mleko", expires_on=today()))
    db.add(PantryItem(name="Ser", expires_on=today() - timedelta(days=2)))
    db.add(PantryItem(name="Mąka", expires_on=today() + timedelta(days=30)))
    db.add(PantryItem(name="Stare", expires_on=today() - timedelta(days=30)))
    db.commit()
    assert notify.run_slot(db, "morning", today()) == 1
    assert sent[0]["title"] == "Domownik: spiżarnia"
    assert sent[0]["body"] == "🥫 Spiżarnia: mleko (dziś), ser (po terminie)"
    # alone it waits 3 days
    assert notify.run_slot(db, "morning", today() + timedelta(days=1)) == 0


def test_finish_shopping_to_pantry(auth_client, db):
    auth_client.post("/zakupy", data={"text": "ser żółty, mrożone pierogi, ryż"})
    for s in db.scalars(select(ShoppingItem)):
        auth_client.post(f"/zakupy/{s.id}/check")
    auth_client.post("/zakupy/zakoncz", data={"to_pantry": "on"})
    places = {p.name: p.place for p in db.scalars(select(PantryItem))}
    assert places == {"Ser żółty": "fridge", "Mrożone pierogi": "freezer", "Ryż": "pantry"}


def test_same_ingredient_adds_up_polish_first_letter_too(auth_client, db):
    """Two recipes with '250 g śmietany' → one row with 500 g; SQLite's ASCII lower() used to miss 'Ś'."""
    a = _recipe(db, "Sos", "250 g śmietany, min. 30% tłuszczu\n1 cebula")
    b = _recipe(db, "Tarta", "250 g śmietany, min. 30% tłuszczu\n2 jajka\n250 g mąki")
    needs = {n.name: n for n in kitchen.needs([a, b], [])}
    assert needs["Śmietana, min. 30% tłuszczu"].entry == "500 g śmietana, min. 30% tłuszczu"
    assert needs["Śmietana, min. 30% tłuszczu"].sources == ["Sos", "Tarta"]

    for rec in (a, b):  # one recipe after the other, as in the report
        auth_client.post("/kuchnia/do-zakupow", data={"entry": rec.ingredient_list}, headers=HX)
    rows = db.scalars(select(ShoppingItem).where(ShoppingItem.name.like("%mietan%"))).all()
    assert [(r.name, r.qty) for r in rows] == [("Śmietana, min. 30% tłuszczu", "500 g")]


def test_recipe_forms_meet_on_the_list(auth_client, db):
    """'5 jajek' and '2 jajka', 'masła' and 'masło': one row each, quantities added, the list's own form."""
    auth_client.post("/zakupy", data={"text": "masło, 2 jajka, mleka 250 g"}, headers=HX)
    entries = ["5 jajek", "100 g masła", "250 g mleka", "1 laska cynamonu", "2 łyżki mąki", "1 łyżka mąki"]
    r = auth_client.post("/kuchnia/do-zakupow", data={"entry": entries}, headers=HX)
    assert r.status_code == 200
    rows = {s.name: s.qty for s in db.scalars(select(ShoppingItem))}
    assert rows == {"Masło": "100 g", "Jajka": "7", "Mleko": "500 g", "Cynamon": "1 laska", "Mąka": "3 łyżki"}


def test_unknown_words_meet_by_stem_and_take_the_lists_spelling(auth_client, db):
    """'rabarbaru' is not in the lexicon: it still meets 'Rabarbar', and a new row takes the spelling used before."""
    auth_client.post("/zakupy", data={"text": "rabarbar"}, headers=HX)
    auth_client.post("/kuchnia/do-zakupow", data={"entry": ["500 g rabarbaru"]}, headers=HX)
    assert [(s.name, s.qty) for s in db.scalars(select(ShoppingItem))] == [("Rabarbar", "500 g")]

    db.query(ShoppingItem).delete()
    db.add(ShoppingItem(name="Tahini", bought_on=today(), section="inne"))  # bought once
    db.commit()
    auth_client.post("/kuchnia/do-zakupow", data={"entry": ["2 łyżki tahiny"]}, headers=HX)
    row = db.scalar(select(ShoppingItem).where(ShoppingItem.bought_on.is_(None)))
    assert (row.name, row.qty) == ("Tahini", "2 łyżki")


def test_shopping_page_picks_a_recipe(auth_client, db):
    """/zakupy → '📖 Z przepisu': recipes with ingredients listed, search narrows, one opens its ingredients."""
    rec = _recipe(db)
    _recipe(db, name="Pusty", ingredients=None)
    _recipe(db, name="Bigos", ingredients="1 kg kapusty\n500 g kiełbasy")
    assert "Z przepisu" in auth_client.get("/zakupy").text
    win = auth_client.get("/kuchnia/do-zakupow/przepis", headers=HX).text
    assert "Naleśniki" in win and "Bigos" in win and "Pusty" not in win
    assert f'hx-get="/kuchnia/przepisy/{rec.id}/zakupy"' in win and "Cookidoo" in win
    only = auth_client.get("/kuchnia/do-zakupow/przepis?q=kapust", headers={**HX, "HX-Target": "pick-recipe-list"}).text
    assert "Bigos" in only and "Naleśniki" not in only and "Cookidoo" not in only
    assert "Nic nie pasuje" in auth_client.get("/kuchnia/do-zakupow/przepis?q=xyz", headers=HX).text


def test_cookidoo_link_from_shopping_opens_ingredients(auth_client, db, monkeypatch):
    from domownik import cookidoo

    url = "https://cookidoo.pl/recipes/recipe/pl-PL/r1"
    got = type(
        "Got",
        (),
        {"url": url, "name": "Zupa", "ingredients": ["2 marchewki"], "minutes": 20, "servings": 4, "tags": []},
    )
    monkeypatch.setattr(cookidoo, "load", lambda u: got)
    r = auth_client.post("/kuchnia/do-zakupow/cookidoo", data={"link": url}, headers=HX)
    assert r.status_code == 200 and "Składniki: Zupa" in r.text and "2 marchewki" in r.text
    r = auth_client.post("/kuchnia/do-zakupow/cookidoo", data={"link": url}, headers=HX)  # second time: no duplicate
    assert len(db.scalars(select(Recipe)).all()) == 1
    assert auth_client.post("/kuchnia/do-zakupow/cookidoo", data={"link": "nope"}, headers=HX).status_code == 400


def test_auto_opened_shop_window_does_not_reopen(auth_client, db):
    """After a Cookidoo import the page opens with ?zakupy=1; the window drops it, so the refresh after adding
    (dm-added reloads the page under the window) does not open it again."""
    rec = _recipe(db)
    page = auth_client.get(f"/kuchnia/przepisy/{rec.id}?zakupy=1").text
    assert 'hx-trigger="click, load"' in page and "history.replaceState" in page
    assert "history.replaceState" not in auth_client.get(f"/kuchnia/przepisy/{rec.id}").text
