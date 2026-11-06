"""Recipe from a Cookidoo link: JSON-LD on the public page → a recipe here → 'do zakupów'."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from domownik import cookidoo
from domownik.models import Recipe

HX = {"HX-Request": "true"}
URL = "https://cookidoo.pl/recipes/recipe/pl-PL/r59322"


def _page(**over) -> str:
    ld = {
        "@context": "http://schema.org/",
        "@type": "Recipe",
        "name": "Bułki pełnoziarniste",
        "totalTime": "PT1H10M",
        "recipeYield": "12 sztuk",
        "recipeCategory": ["Chleb i bułki"],
        "recipeIngredient": ["100 g ziaren pszenicy", "1 &frac12; łyżeczki soli", "1 - 2 ząbki czosnku", "½ cebuli"],
    } | over
    rating = {"@context": "http://schema.org", "@type": "AggregateRating", "ratingValue": 4.3}
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(rating)}</script>'
        f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script></head></html>'
    )


def test_find_url_in_shared_text():
    shared = f"Zobacz ten przepis na Cookidoo®: Bułki {URL}?utm_source=app."
    assert cookidoo.find_url(shared) == f"{URL}?utm_source=app"
    assert cookidoo.find_url("cookidoo.pl/recipes/recipe/pl-PL/r1") == "https://cookidoo.pl/recipes/recipe/pl-PL/r1"
    assert cookidoo.find_url("https://cookidoo.thermomix.com/recipes/recipe/en/r1")
    for bad in ("", "https://example.com/r1", "https://cookidoo.pl.evil.com/x", "http://127.0.0.1/cookidoo.pl"):
        with pytest.raises(cookidoo.CookidooError):
            cookidoo.find_url(bad)


def test_parse_json_ld():
    r = cookidoo.parse(_page(), URL)
    assert r.name == "Bułki pełnoziarniste"
    assert r.ingredients == ["100 g ziaren pszenicy", "1,5 łyżeczki soli", "2 ząbki czosnku", "0,5 cebuli"]
    assert (r.minutes, r.servings, r.tags) == (70, 12, ["chleb i bułki"])
    with pytest.raises(cookidoo.CookidooError, match="Nie znalazłem przepisu"):
        cookidoo.parse("<html>Zaloguj się</html>", URL)


def test_import_creates_recipe_once_and_opens_shopping(auth_client, db, monkeypatch):
    calls = []
    monkeypatch.setattr(cookidoo, "fetch", lambda url: calls.append(url) or _page())
    r = auth_client.post(
        "/kuchnia/przepisy/cookidoo", data={"link": f"Zobacz: {URL}?x=1"}, headers=HX, follow_redirects=False
    )
    assert r.status_code == 303
    rec = db.scalar(select(Recipe))
    assert r.headers["HX-Redirect"] == f"/kuchnia/przepisy/{rec.id}?zakupy=1"
    assert rec.link == URL and rec.servings == 12 and rec.minutes == 70
    assert rec.tags == "cookidoo, chleb i bułki"
    assert rec.ingredient_list[1] == "1,5 łyżeczki soli"
    page = auth_client.get(f"/kuchnia/przepisy/{rec.id}?zakupy=1").text
    assert 'hx-trigger="click, load"' in page and "otwórz w Cookidoo" in page
    assert 'hx-trigger="click, load"' not in auth_client.get(f"/kuchnia/przepisy/{rec.id}").text
    # the same link again → the same recipe, nothing fetched
    r = auth_client.post("/kuchnia/przepisy/cookidoo", data={"link": URL}, headers=HX, follow_redirects=False)
    assert r.headers["HX-Redirect"] == f"/kuchnia/przepisy/{rec.id}?zakupy=1"
    assert len(calls) == 1 and len(db.scalars(select(Recipe)).all()) == 1


def test_import_errors_are_messages(auth_client, db, monkeypatch):
    r = auth_client.post("/kuchnia/przepisy/cookidoo", data={"link": "https://example.com"}, headers=HX)
    assert r.status_code == 400 and "Cookidoo" in r.text

    def offline(url):
        raise cookidoo.CookidooError("Nie mogę połączyć się z Cookidoo")

    monkeypatch.setattr(cookidoo, "fetch", offline)
    r = auth_client.post("/kuchnia/przepisy/cookidoo", data={"link": URL}, headers=HX)
    assert r.status_code == 400 and "połączyć" in r.text
    assert db.scalar(select(Recipe)) is None


def test_recipes_page_has_the_link_box(auth_client):
    assert 'action="/kuchnia/przepisy/cookidoo"' in auth_client.get("/kuchnia/przepisy").text
