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
    assert recurring._pretty("NETFLIX.COM") == "Netflix.com" and recurring._pretty("HBO MAX") == "HBO Max"
    assert recurring._pretty("JAN KOWALSKI") == "Jan Kowalski" and recurring._pretty("NETIA S.A.") == "Netia S.A."


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


def test_one_payee_under_two_names(db):
    for i in range(6):
        desc = "BIURO RACHUNKOWE VERITAX ANNA KOWALSKA, FV PRZELEW" if i < 3 else "BIURO RACHUNKOWE VERITAX, FV PRZELEW"
        _line(db, T - timedelta(days=5 + 30 * (5 - i)), 246, desc)
    db.commit()
    found = recurring.find(db, T)
    assert len(found) == 1 and found[0].charges == 6 and not found[0].ended


def _item(db):
    from domownik.models import Item

    it = Item(name="Dom", kind="house")
    db.add(it)
    db.commit()
    return it


def test_linked_to_payments_already_in_domownik(db):
    from domownik.models import Task, Transaction

    it = _item(db)
    play = Task(
        item_id=it.id,
        title="Play – telefon",
        category="payment",
        amount_gr=23300,
        direction="expense",
        recurrence={"mode": "monthly", "day": 19},
    )
    party = Task(
        item_id=it.id,
        title="Dzień Dziecka",
        category="other",
        amount_gr=20000,
        direction="expense",
        recurrence={"mode": "once"},
    )
    rent = Task(
        item_id=it.id,
        title="Czynsz",
        category="payment",
        amount_gr=90000,
        direction="expense",
        recurrence={"mode": "monthly", "day": 10},
    )
    db.add_all([play, party, rent])
    db.flush()
    _monthly(db, "PLAY, F/1 PRZELEW", [232.6] * 6)  # ~ the same sum and a shared word
    _monthly(db, "GOTOWE STRATEGIE SFIO, WPŁATA PRZELEW", [200] * 6)  # the same sum, nothing in common
    _monthly(db, "WSPÓLNOTA MIESZKANIOWA, OPŁATA PRZELEW", [922.37] * 6)
    for ln in db.query(BankLine).filter(BankLine.description.like("WSPÓLNOTA%")):  # booked to the rent at import
        tr = Transaction(booked_on=ln.booked_on, amount_gr=ln.amount_gr, task_id=rent.id)
        db.add(tr)
        db.flush()
        ln.transaction_id = tr.id
    db.commit()
    found = {s.name: s for s in recurring.find(db, T)}
    recurring.link_tasks(db, list(found.values()))
    assert found["Play"].task == play
    assert found["Gotowe Strategie Sfio"].task is None
    assert found["Wspólnota Mieszkaniowa"].task == rent  # by the bank booking, whatever the name


def test_actions(auth_client, db):
    from domownik.models import StandingMark, Task, Todo

    _item(db)
    _monthly(db, "HBO MAX ZAKUP PRZY UŻYCIU KARTY - INTERNET", [19.99] * 6)
    _monthly(db, "JAN KOWALSKI, KIESZONKOWE PRZELEW", [100] * 6)
    _monthly(db, "NETIA S.A., 571 PRZELEW", [40.11] * 5, last=T - timedelta(days=200))
    db.commit()
    keys = {s.name: s.key for s in recurring.find(db, T)}
    page = auth_client.get("/budzet/stale").text
    assert "Zrób z tego opłatę" in page and "Tak, zakończone" in page

    auth_client.post("/budzet/stale", data={"key": keys["Jan Kowalski"], "co": "pomin"})
    auth_client.post("/budzet/stale", data={"key": keys["Netia S.A."], "co": "zakonczone"})
    page = auth_client.get("/budzet/stale").text
    assert "Ukryte (2)" in page and "Chyba zakończone" not in page
    assert "≈ 19,99\xa0zł" in page  # the skipped one is out of the sum
    auth_client.post("/budzet/stale", data={"key": keys["Jan Kowalski"], "co": "przywroc"})
    assert db.query(StandingMark).count() == 1

    r = auth_client.post("/budzet/stale", data={"key": keys["HBO Max"], "co": "zrezygnuj"}, follow_redirects=False)
    assert "dodano=" in r.headers["location"]
    assert db.query(Todo).one().title == "Zrezygnuj z: HBO Max"

    r = auth_client.post("/budzet/stale", data={"key": keys["HBO Max"], "co": "oplata"}, follow_redirects=False)
    t = db.query(Task).one()
    assert r.headers["location"] == f"/tasks/{t.id}/edit"
    assert (t.title, t.amount_gr, t.category, t.autopay, t.recurrence["mode"]) == (
        "HBO Max",
        1999,
        "payment",
        True,
        "monthly",
    )
    assert t.next_due_date >= date.today()
    # now it is a payment: the page says so and does not offer another one
    assert "✓ w opłatach" in auth_client.get("/budzet/stale").text
    assert auth_client.post("/budzet/stale", data={"key": "nie ma", "co": "pomin"}).status_code == 404


def test_ended_comes_back_with_a_new_charge(db):
    from domownik.models import StandingMark

    _monthly(db, "NETIA S.A., 571 PRZELEW", [40.11] * 5, last=T - timedelta(days=200))
    db.commit()
    s = recurring.find(db, T)[0]
    db.add(StandingMark(key=s.key, state="ended", last_on=s.last_on))
    _line(db, T - timedelta(days=2), 40.11, "NETIA S.A., 572 PRZELEW")
    db.commit()
    assert recurring.find(db, T)[0].last_on > s.last_on  # the router drops the mark then (test_actions covers it)
