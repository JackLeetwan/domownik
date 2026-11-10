"""💳 Loyalty cards: the number, the kind of barcode, the page shown at the till."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from domownik import brands, loyalty, webfetch
from domownik.models import LoyaltyCard


def _png(color=(200, 16, 46), size=(180, 180), alpha_margin=0) -> bytes:
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    inner = Image.new("RGBA", (size[0] - 2 * alpha_margin, size[1] - 2 * alpha_margin), (*color, 255))
    for x in range(inner.width // 4):  # a white stripe: logos have white in them, it must not win
        for y in range(inner.height):
            inner.putpixel((x, y), (255, 255, 255, 255))
    im.paste(inner, (alpha_margin, alpha_margin))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class _Web:
    """webfetch.get stand-in: url → (body, content type); anything else → 404."""

    def __init__(self, pages: dict[str, tuple[bytes | str, str]]):
        self.pages, self.asked = pages, []

    def __call__(self, url, *, max_bytes, timeout=15.0, what="Strona"):
        self.asked.append(url)
        if url not in self.pages:
            raise webfetch.FetchError("Nie ma takiej strony (404) – sprawdź link")
        body, ctype = self.pages[url]
        body = body.encode() if isinstance(body, str) else body
        return webfetch.Fetched(body, ctype, url, "utf-8")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    web = _Web({})
    monkeypatch.setattr(webfetch, "get", web)
    return web


def test_kind_is_guessed_from_the_number():
    assert loyalty.clean("590 1234 12345 7", None) == ("5901234123457", "ean13")  # valid check digit
    assert loyalty.clean("5901234123458", None) == ("5901234123458", "code128")  # mistyped → still drawable
    assert loyalty.clean("96385074", None) == ("96385074", "ean8")
    assert loyalty.clean("036000291452", None) == ("036000291452", "upca")
    assert loyalty.clean("AB-12 34", None) == ("AB-1234", "code128")


def test_scanned_is_kept_exactly():
    assert loyalty.clean("AB 12", "code_128") == ("AB 12", "code128")
    assert loyalty.clean("https://x.pl/k?a=1 b", "qr_code") == ("https://x.pl/k?a=1 b", "qr")
    assert loyalty.clean("12345", "data_matrix")[1] == "code128"  # a kind we cannot draw: Code 128


def test_bad_numbers():
    with pytest.raises(loyalty.CardError, match="Wpisz numer"):
        loyalty.clean("  ", None)
    with pytest.raises(loyalty.CardError, match="polskich znaków"):
        loyalty.clean("ŻABKA1", None)
    with pytest.raises(loyalty.CardError, match="za długi"):
        loyalty.clean("1" * 200, None)


@pytest.mark.parametrize("code,fmt", [("5901234123457", "ean13"), ("ABC123", "code128"), ("x y", "qr")])
def test_svg_scales(code, fmt):
    s = loyalty.svg(code, fmt)
    assert s.startswith("<svg") and "viewBox=" in s and "mm" not in s and "<!--" not in s
    assert s.count("class=") >= 1 and 'class="segno"' not in s


def test_pages(auth_client, db):
    assert "💳 Karty" in auth_client.get("/zakupy").text
    page = auth_client.get("/zakupy/karty").text
    assert "Dodaj kartę" in page and "data-scan" in page
    r = auth_client.post(
        "/zakupy/karty", data={"name": "Rossmann", "code": "590 1234 12345 7", "fmt": ""}, follow_redirects=False
    )
    assert r.status_code == 303
    c = db.query(LoyaltyCard).one()
    assert (c.code, c.fmt, c.used_at) == ("5901234123457", "ean13", None)
    page = auth_client.get(f"/zakupy/karty/{c.id}").text
    assert "<svg" in page and "5901234123457" in page
    db.refresh(c)
    assert c.used_at is not None
    r = auth_client.post("/zakupy/karty", data={"name": "", "code": "1"})
    assert r.status_code == 400 and "nazwę sklepu" in r.text
    auth_client.post(f"/zakupy/karty/{c.id}/edit", data={"name": "Rossmann", "note": "Pauliny"})
    db.refresh(c)
    assert c.note == "Pauliny"
    auth_client.post(f"/zakupy/karty/{c.id}/usun")
    assert db.query(LoyaltyCard).count() == 0
    assert auth_client.get(f"/zakupy/karty/{c.id}").status_code == 404


def test_known_shops_and_sites():
    assert brands.known_site("Rossmann") == "rossmann.pl"
    assert brands.known_site("IKEA Family") == "ikea.com"
    assert brands.known_site("Moja Biedronka") == "biedronka.pl"
    assert brands.known_site("Karta Rossmann Club") == "rossmann.pl"  # a known name inside
    assert brands.known_site("Żabka") == "zabka.pl"  # Polish letters as typed
    assert brands.known_site("Sklepik pani Basi") is None
    assert brands.host_of("https://www.rossmann.pl/promocje?x=1") == "rossmann.pl"
    assert brands.host_of("leroymerlin.pl") == "leroymerlin.pl" and brands.host_of("nie strona") is None


def test_logo_and_colour_from_the_site(_offline):
    _offline.pages.update(
        {
            "https://rossmann.pl/": (
                '<html><head><title>Rossmann</title><meta name="theme-color" content="#FFFFFF">'
                '<link rel="icon" href="/fav.ico"><link rel="apple-touch-icon" sizes="180x180" href="/apple.png">'
                '<link rel="manifest" href="/m.json"></head></html>',
                "text/html",
            ),
            "https://rossmann.pl/m.json": ('{"theme_color": "#c8102e", "icons": []}', "application/json"),
            "https://rossmann.pl/apple.png": (_png(alpha_margin=20), "image/png"),
        }
    )
    b = brands.look_up("Rossmann")
    assert b.site == "rossmann.pl" and b.color == "#c8102e"  # white theme-color skipped, the manifest's taken
    im = Image.open(io.BytesIO(b.logo))
    assert im.size == (brands.LOGO_PX, brands.LOGO_PX)
    assert im.getchannel("A").getbbox() == (0, 0, brands.LOGO_PX, brands.LOGO_PX)  # transparent margin trimmed


def test_colour_from_the_logo_and_a_blocked_site(_offline):
    google = brands.FALLBACKS[0].format(domain="hebe.pl")
    _offline.pages[google] = (_png((230, 37, 139), size=(16, 16)), "image/png")
    b = brands.look_up("Hebe")  # hebe.pl itself: refused (not in pages)
    assert b.site == "hebe.pl" and b.logo is None  # 16 px is too small for a logo …
    r, g, bl = brands._rgb(b.color)
    assert abs(r - 230) < 8 and abs(g - 37) < 8 and abs(bl - 139) < 8  # … but its colour is right
    assert brands.text_on("#fff100") == "#1b1b1b" and brands.text_on("#c8102e") == "#fff"


def test_unknown_shop_is_guessed_only_when_the_page_says_so(_offline):
    _offline.pages["https://sklepikbasi.pl/"] = ("<title>Zupełnie coś innego</title>", "text/html")
    assert brands.look_up("Sklepik Basi") is None
    _offline.pages["https://sklepikbasi.pl/"] = ("<title>Sklepik Basi – warzywa</title>", "text/html")
    assert brands.look_up("Sklepik Basi").site == "sklepikbasi.pl"


def test_card_gets_brand_and_own_logo(auth_client, db, _offline, tmp_path, monkeypatch):
    from domownik.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    _offline.pages.update(
        {
            "https://ikea.com/": (
                '<link rel="apple-touch-icon" href="/a.png"><meta name="theme-color" content="#0058a3">',
                "text/html",
            ),
            "https://ikea.com/a.png": (_png((0, 88, 163)), "image/png"),
        }
    )
    auth_client.post("/zakupy/karty", data={"name": "IKEA Family", "code": "6275980000012345678"})
    c = db.query(LoyaltyCard).one()
    assert (c.site, c.color) == ("ikea.com", "#0058a3") and loyalty.logo_file(c) is not None
    page = auth_client.get("/zakupy/karty").text
    assert "--brand: #0058a3" in page and f"/zakupy/karty/{c.id}/logo" in page
    r = auth_client.get(f"/zakupy/karty/{c.id}/logo")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    old = loyalty.logo_file(c)

    # one's own logo (a photo of the card) replaces it; the old file goes
    r = auth_client.post(
        f"/zakupy/karty/{c.id}/edit",
        data={"name": "IKEA Family", "color": "#123456"},
        files={"files": ("karta.jpg", _png((10, 200, 10), size=(800, 500)), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.refresh(c)
    assert c.color == "#123456" and not old.exists()
    assert Image.open(loyalty.logo_file(c)).size == loyalty.OWN_LOGO_BOX
    r = auth_client.post(
        f"/zakupy/karty/{c.id}/edit", data={"name": "IKEA Family"}, files={"files": ("x.png", b"nope", "image/png")}
    )
    assert r.status_code == 400 and "To nie jest obrazek" in r.text

    path = loyalty.logo_file(c)
    auth_client.post(f"/zakupy/karty/{c.id}/usun")
    assert not path.exists()


def test_no_site_found_is_said(auth_client, db, tmp_path, monkeypatch):
    from domownik.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    auth_client.post("/zakupy/karty", data={"name": "Sklepik Basi", "code": "123"})
    c = db.query(LoyaltyCard).one()
    assert c.color is None and c.logo_path is None  # saved anyway, in its own colour
    r = auth_client.post(f"/zakupy/karty/{c.id}/edit", data={"name": "Sklepik Basi", "fetch": "1"})
    assert r.status_code == 400 and "wpisz jej adres" in r.text


def test_a_services_grey_stand_in_is_not_a_logo(_offline):
    google, horse = (f.format(domain="kaufland.pl") for f in brands.FALLBACKS)
    _offline.pages[google] = (_png((225, 13, 0), size=(16, 16)), "image/png")
    grey = Image.new("RGBA", (256, 256), (220, 220, 220, 255))
    buf = io.BytesIO()
    grey.save(buf, "PNG")
    _offline.pages[horse] = (buf.getvalue(), "image/png")
    b = brands.look_up("Kaufland")
    assert b.logo is None and b.color is not None and brands._rgb(b.color)[0] > 200  # red, from the favicon
