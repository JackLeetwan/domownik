"""🔁 Standing payments found in the bank statements."""

from __future__ import annotations

from datetime import date, timedelta

from domownik import recurring
from domownik.models import BankLine

T = date(2026, 11, 10)


def _line(db, day, zl, desc):
    db.add(BankLine(ref=f"{day}{zl}{desc}"[:40], booked_on=day, amount_gr=-round(zl * 100), description=desc))


def _monthly(db, desc, prices, *, last=T - timedelta(days=5), every=30):
    for i, p in enumerate(reversed(prices)):
        _line(db, last - timedelta(days=every * i + (i % 3)), p, desc)


def test_payee_and_names():
    assert recurring.payee("NETFLIX.COM ZAKUP PRZY UŻYCIU KARTY - INTERNET") == "NETFLIX.COM"
    assert recurring.payee("PLAY, F/50557903/11/25 PRZELEW WEWNĘTRZNY WYCHODZĄCY 7311") == "PLAY"
    assert recurring._pretty("NETFLIX.COM") == "Netflix.com" and recurring._pretty("HBO MAX") == "HBO MAX"


def test_finds_subscriptions_bills_and_price_rises(db):
    _monthly(db, "NETFLIX.COM ZAKUP PRZY UŻYCIU KARTY - INTERNET", [49] * 6 + [55] * 4)
    _monthly(db, "PLAY, F/1/25 PRZELEW WEWNĘTRZNY WYCHODZĄCY", [232.6] * 8)
    _monthly(db, "ENEA, FAKTURA VAT NR P/1 PRZELEW ZEWNĘTRZNY", [210, 260, 190, 240, 230, 280])  # a bill: varies
    _monthly(db, "NETIA S.A., 571 PRZELEW WEWNĘTRZNY", [40.11] * 5, last=T - timedelta(days=200))  # stopped
    _monthly(db, "OC AUTA, POLISA PRZELEW", [1200] * 2, last=T - timedelta(days=20), every=365)
    for i in range(40):  # a shop: often, no rhythm, any sum
        _line(db, T - timedelta(days=i * 7 + i % 4), 30 + (i * 37) % 90, "BIEDRONKA ZAKUP PRZY UŻYCIU KARTY W KRAJU")
    for i in range(6):  # cash from an ATM every month is not a payment
        _line(db, T - timedelta(days=30 * i), 500, "MBANK WYPŁATA GOTÓWKI W BANKOMACIE WŁASNYM")
    db.commit()
    found = {s.name: s for s in recurring.find(db, T)}
    assert set(found) == {"Netflix.com", "Play", "Enea", "Netia S.A.", "OC Auta"}
    nf = found["Netflix.com"]
    assert (nf.usual_gr, nf.before_gr, nf.dearer_gr, nf.steady, nf.ended) == (5500, 4900, 600, True, False)
    assert found["Play"].dearer_gr == 0 and found["Play"].cadence == "co miesiąc"
    assert not found["Enea"].steady and found["Enea"].dearer_gr == 0
    assert found["Netia S.A."].ended
    oc = found["OC Auta"]
    assert oc.cadence == "co rok" and oc.monthly_gr == 10000
    assert [s.name for s in recurring.find(db, T)][-1] == "Netia S.A."  # the ended ones last
    _line(db, T - timedelta(days=100), 899, "IKEA ZAKUP PRZY UŻYCIU KARTY W KRAJU")
    _line(db, T - timedelta(days=460), 120, "IKEA ZAKUP PRZY UŻYCIU KARTY W KRAJU")  # a year apart, other sum
    db.commit()
    assert "Ikea" not in {s.name for s in recurring.find(db, T)}


def test_page(auth_client, db):
    assert (
        "import wyciągu" in auth_client.get("/budzet/stale").text.lower()
        or "Import z banku" in auth_client.get("/budzet/stale").text
    )
    _monthly(db, "NETFLIX.COM ZAKUP PRZY UŻYCIU KARTY - INTERNET", [49] * 6 + [55] * 4)
    _monthly(db, "NETIA S.A., 571 PRZELEW", [40.11] * 5, last=T - timedelta(days=200))
    db.commit()
    page = auth_client.get("/budzet/stale").text
    assert "Netflix.com" in page and "wcześniej 49" in page and "Chyba zakończone (1)" in page
    assert "🔁 Stałe płatności" in auth_client.get("/budzet").text
