"""A recipe from a link (Cookidoo or any recipe site): JSON-LD on the public page → a recipe here → 'do zakupów'."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from domownik import recipe_link, webfetch
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
    assert recipe_link.find_url(shared) == f"{URL}?utm_source=app"
    assert recipe_link.find_url("cookidoo.pl/recipes/recipe/pl-PL/r1") == "https://cookidoo.pl/recipes/recipe/pl-PL/r1"
    assert recipe_link.find_url("www.kwestiasmaku.com/przepis/bigos") == "https://www.kwestiasmaku.com/przepis/bigos"
    # a shared text with two links: the recipe site, not the app store one after it
    assert recipe_link.find_url("Bigos https://www.kwestiasmaku.com/przepis/bigos (z aplikacji)").endswith("/bigos")
    for bad in ("", "przepis na bigos", "ftp://example.com/x", "javascript:alert(1)"):
        with pytest.raises(recipe_link.RecipeLinkError):
            recipe_link.find_url(bad)
    assert recipe_link.site("https://www.kwestiasmaku.com/x") == "kwestiasmaku" and recipe_link.site(URL) == "cookidoo"


def test_never_into_the_home_network(monkeypatch):
    import httpx

    for host in ("127.0.0.1", "192.168.8.1", "10.0.0.5", "[::1]"):
        with pytest.raises(recipe_link.RecipeLinkError, match="sieci lokalnej"):
            recipe_link._check(httpx.Request("GET", f"http://{host}/przepis"))
    monkeypatch.setattr(webfetch.socket, "getaddrinfo", lambda h, p: [(2, 1, 6, "", ("192.168.1.10", 0))])
    with pytest.raises(recipe_link.RecipeLinkError, match="sieci lokalnej"):  # a public name pointing home
        recipe_link._check(httpx.Request("GET", "https://przepisy.example.com/x"))


def test_any_site_with_steps():
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": "Bigos"},
            {
                "@type": ["Recipe"],
                "name": "Bigos staropolski",
                "recipeIngredient": ["1 kg kapusty kiszonej", "300 g kiełbasy"],
                "recipeInstructions": [
                    {
                        "@type": "HowToSection",
                        "name": "Przygotowanie",
                        "itemListElement": [
                            {"@type": "HowToStep", "text": "Kapustę odcisnąć."},
                            {"@type": "HowToStep", "text": "Kiełbasę pokroić &amp; podsmażyć."},
                        ],
                    },
                    {"@type": "HowToStep", "text": "Dusić 2 godziny."},
                ],
                "recipeYield": ["6", "6 porcji"],
                "totalTime": "PT2H30M",
            },
        ],
    }
    page = f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'
    r = recipe_link.parse(page, "https://www.kwestiasmaku.com/przepis/bigos")
    assert r.ingredients == ["1 kg kapusty kiszonej", "300 g kiełbasy"]
    assert r.steps == ["Przygotowanie:", "Kapustę odcisnąć.", "Kiełbasę pokroić & podsmażyć.", "Dusić 2 godziny."]
    assert (r.servings, r.minutes) == (6, 150)
    one_text = {
        "@type": "Recipe",
        "name": "Placki",
        "recipeIngredient": "mąka",
        "recipeInstructions": "<p>Wymieszać.</p><p>Smażyć.</p>",
    }
    r = recipe_link.parse(f'<script type="application/ld+json">{json.dumps(one_text)}</script>', "https://x.pl/p")
    assert r.steps == ["Wymieszać.", "Smażyć."] and r.ingredients == ["mąka"]
    with pytest.raises(recipe_link.RecipeLinkError, match="standardowy"):
        recipe_link.parse("<html>Blog</html>", "https://x.pl/p")


def test_parse_json_ld():
    r = recipe_link.parse(_page(), URL)
    assert r.name == "Bułki pełnoziarniste"
    assert r.ingredients == ["100 g ziaren pszenicy", "1,5 łyżeczki soli", "2 ząbki czosnku", "0,5 cebuli"]
    assert (r.minutes, r.servings, r.tags) == (70, 12, ["chleb i bułki"])
    with pytest.raises(recipe_link.RecipeLinkError, match="Nie znalazłem przepisu"):
        recipe_link.parse("<html>Zaloguj się</html>", URL)


def test_import_creates_recipe_once_and_opens_shopping(auth_client, db, monkeypatch):
    calls = []
    monkeypatch.setattr(recipe_link, "fetch", lambda url: calls.append(url) or _page())
    r = auth_client.post(
        "/kuchnia/przepisy/z-linku", data={"link": f"Zobacz: {URL}?x=1"}, headers=HX, follow_redirects=False
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
    r = auth_client.post("/kuchnia/przepisy/z-linku", data={"link": URL}, headers=HX, follow_redirects=False)
    assert r.headers["HX-Redirect"] == f"/kuchnia/przepisy/{rec.id}?zakupy=1"
    assert len(calls) == 1 and len(db.scalars(select(Recipe)).all()) == 1


def test_import_errors_are_messages(auth_client, db, monkeypatch):
    r = auth_client.post("/kuchnia/przepisy/z-linku", data={"link": "przepis na bigos"}, headers=HX)
    assert r.status_code == 400 and "link do przepisu" in r.text

    def offline(url):
        raise recipe_link.RecipeLinkError("Nie mogę połączyć się z Cookidoo")

    monkeypatch.setattr(recipe_link, "fetch", offline)
    r = auth_client.post("/kuchnia/przepisy/z-linku", data={"link": URL}, headers=HX)
    assert r.status_code == 400 and "połączyć" in r.text
    assert db.scalar(select(Recipe)) is None


def test_recipes_page_has_the_link_box(auth_client):
    page = auth_client.get("/kuchnia/przepisy").text
    assert 'action="/kuchnia/przepisy/z-linku"' in page and "dowolna strona" in page


def test_pages_without_json_ld():
    # Kwestia Smaku / Moje Wypieki: a plain list under "Składniki"; a menu link "Składniki" is not it
    page = """<html><head><meta property="og:title" content="Sernik | Moja strona"></head><body>
      <nav><a href="/s">Składniki</a><ul><li>Kasze</li></ul></nav>
      <h1>Sernik krakowski</h1><span>819</span>
      <p><u>Składniki na spód:</u></p><ul><li>250 g mąki</li><li>1 jajko</li></ul>
      <p>Wymieszać i upiec spód.</p>
      <p><u>Składniki na masę:</u></p><ul><li>1 kg twarogu</li><li>1 &frac12; szklanki cukru</li></ul>
      <h3>Przygotowanie</h3><ol><li>Twaróg zmiksować.</li><li>Piec 60 minut.</li></ol>
      <h3>Komentarze</h3><ul><li>Pyszne!</li><li>12</li></ul>
    </body></html>"""
    r = recipe_link.parse(page, "https://www.mojewypieki.com/przepis/sernik")
    assert r.name == "Sernik krakowski"
    assert r.ingredients == ["250 g mąki", "1 jajko", "1 kg twarogu", "1,5 szklanki cukru"]
    assert r.steps == ["Twaróg zmiksować.", "Piec 60 minut."]
    # Ania Gotuje: microdata
    page = """<h1>Bigos | AniaGotuje.pl</h1><article itemscope itemtype="https://schema.org/Recipe">
      <meta itemprop="totalTime" content="PT2H"><meta itemprop="recipeYield" content="5 porcji">
      <ul><li itemprop="recipeIngredient">1 kg <b>kapusty</b></li><li itemprop="recipeIngredient">2 cebule</li></ul>
      <div itemprop="recipeInstructions"><span itemprop="text">Kapustę pokroić.</span></div></article>"""
    r = recipe_link.parse(page, "https://aniagotuje.pl/przepis/bigos")
    assert (r.name, r.ingredients, r.steps, r.minutes, r.servings) == (
        "Bigos",
        ["1 kg kapusty", "2 cebule"],
        ["Kapustę pokroić."],
        120,
        5,
    )
