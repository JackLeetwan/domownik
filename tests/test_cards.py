"""💳 Loyalty cards: the number, the kind of barcode, the page shown at the till."""

from __future__ import annotations

import pytest

from domownik import loyalty
from domownik.models import LoyaltyCard


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
