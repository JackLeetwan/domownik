from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from domownik import supplements as supl
from domownik import webpush
from domownik.models import PushSubscription, ShoppingItem, Supplement, SupplementDose, Transaction, User
from domownik.security import hash_password

DAY = date(2026, 11, 10)
T8 = datetime(2026, 11, 10, 8, 0)


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []

    def fake_deliver(sub, data, *, urgent=False):
        out.append(json.loads(data) | {"_endpoint": sub.endpoint})
        return True

    monkeypatch.setattr(webpush, "deliver", fake_deliver)
    return out


@pytest.fixture
def paula(db):
    u = User(email="paula@example.com", name="Paula", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


def _device(db, user):
    db.add(PushSubscription(user_id=user.id, endpoint=f"https://push.example.com/{user.id}", p256dh="pk", auth="au"))
    db.commit()


def _three(db, user, at=datetime(2026, 11, 9, 7, 0)):
    out = [
        supl.create(db, user, name="Omega-3", dose="1 kaps.", time="08:00", at=at, pack_size=120, stock=120),
        supl.create(db, user, name="Witamina D3", dose="2000 IU", time="08:00", at=at),
        supl.create(db, user, name="Kreatyna", dose="5 g", time="20:00", at=at, pack_size=100, stock=100),
    ]
    db.commit()
    return out


# ---------------------------------------------------------------- push


def test_one_push_per_hour_then_again_in_the_evening(db, user, sent):
    _device(db, user)
    omega, _d3, _krea = _three(db, user)

    assert supl.run(db, T8 - timedelta(minutes=1)) == 0
    assert supl.run(db, T8) == 1
    assert sent[0]["title"] == "💊 Omega-3, Witamina D3"
    assert sent[0]["body"] == "Omega-3 1 kaps. · Witamina D3 2000 IU"
    assert sent[0]["actions"][0]["title"] == "✓ Wzięte"
    assert supl.run(db, T8 + timedelta(minutes=1)) == 0  # once

    supl.take(db, omega, DAY)
    db.commit()
    assert supl.run(db, datetime(2026, 11, 10, 18, 59)) == 0
    assert supl.run(db, datetime(2026, 11, 10, 19, 0)) == 1  # evening hour: only what is still open
    assert sent[1]["title"] == "💊 Witamina D3"
    assert sent[1]["body"].startswith("Jeszcze nie odhaczone (miało być o 08:00).")

    # kreatyna at 20:00 → its second push 2 h later, not at 19:00
    assert supl.run(db, datetime(2026, 11, 10, 20, 0)) == 1
    assert supl.run(db, datetime(2026, 11, 10, 21, 59)) == 0
    assert supl.run(db, datetime(2026, 11, 10, 22, 0)) == 1
    assert supl.run(db, datetime(2026, 11, 10, 23, 0)) == 0


def test_nothing_when_taken_and_the_next_day_starts_over(db, user, sent):
    _device(db, user)
    omega, d3, _ = _three(db, user)
    supl.take(db, omega, DAY)
    supl.take(db, d3, DAY)
    db.commit()
    assert supl.run(db, T8) == 0
    assert supl.run(db, datetime(2026, 11, 10, 19, 0)) == 0
    assert supl.run(db, T8 + timedelta(days=1)) == 1


def test_late_server_skips_old_push(db, user, sent):
    _device(db, user)
    _three(db, user)
    assert supl.run(db, datetime(2026, 11, 10, 11, 30)) == 0  # 3,5 h late – quietly marked
    assert supl.run(db, datetime(2026, 11, 10, 19, 0)) == 1  # the evening one still comes


def test_added_after_its_hour_does_not_push_today(db, user, sent):
    _device(db, user)
    supl.create(db, user, name="Omega-3", dose=None, time="08:00", at=datetime(2026, 11, 10, 12, 0))
    db.commit()
    assert supl.run(db, datetime(2026, 11, 10, 12, 1)) == 0
    assert supl.run(db, datetime(2026, 11, 10, 19, 0)) == 0
    assert supl.run(db, T8 + timedelta(days=1)) == 1


def test_pushes_go_only_to_the_owner(db, user, paula, sent):
    _device(db, user)
    _device(db, paula)
    _three(db, user)
    supl.run(db, T8)
    assert [s["_endpoint"] for s in sent] == [f"https://push.example.com/{user.id}"]


def test_push_button_ticks_off_and_gives_streak(client, db, user, sent):
    omega, d3, _ = _three(db, user)
    tok = supl.token(user, DAY, [omega, d3])
    r = client.post(f"/n/supl/{tok}")
    assert r.status_code == 200
    assert r.text.startswith("Wzięte: Omega-3, Witamina D3")
    assert db.query(SupplementDose).count() == 2
    assert client.post(f"/n/supl/{tok}").status_code == 200  # twice is harmless
    assert db.query(SupplementDose).count() == 2
    assert client.post("/n/supl/garbage").status_code == 403


# ---------------------------------------------------------------- stock and shopping list


def test_running_out_goes_on_the_list_once(db, user):
    s = supl.create(db, user, name="Kreatyna", dose="5 g", time="08:00", at=T8, pack_size=100, stock=12)
    db.commit()
    assert db.query(ShoppingItem).count() == 0
    supl.take(db, s, DAY)
    supl.take(db, s, DAY + timedelta(days=1))
    db.commit()
    items = db.query(ShoppingItem).all()
    assert [(i.name, i.note) for i in items] == [("Kreatyna", "💊 zostało na ~10 dni")]
    supl.take(db, s, DAY + timedelta(days=2))
    db.commit()
    assert db.query(ShoppingItem).count() == 1
    assert s.stock == 9


def test_untake_gives_stock_back(db, user):
    s = supl.create(db, user, name="Omega", dose=None, time=None, per_take=2, pack_size=120, stock=120)
    supl.take(db, s, DAY)
    assert s.stock == 118
    assert supl.untake(db, s, DAY)
    assert s.stock == 120


def test_bought_pack_restocks_clears_list_and_books_budget(db, user):
    s = supl.create(db, user, name="Kreatyna", dose="5 g", time=None, pack_size=100, stock=5)
    db.commit()
    assert s.restock_listed
    supl.bought_pack(db, s, user, price_gr=8990, day=DAY)
    db.commit()
    assert s.stock == 105 and not s.restock_listed
    assert db.query(ShoppingItem).one().bought_on == DAY
    tr = db.query(Transaction).one()
    assert tr.amount_gr == -8990 and tr.budget_category.name == "Zdrowie"


def test_finish_shopping_restocks(auth_client, db, user):
    s = supl.create(db, user, name="Kreatyna", dose="5 g", time=None, pack_size=100, stock=5)
    db.commit()
    item = db.query(ShoppingItem).one()
    item.checked_at = datetime(2026, 11, 10, 10, 0)
    db.commit()
    assert auth_client.post("/zakupy/zakoncz", data={}).status_code in (200, 303)
    db.refresh(s)
    assert s.stock == 105 and not s.restock_listed


# ---------------------------------------------------------------- streak


def test_streak(db, user):
    omega, d3, _ = _three(db, user, at=datetime(2026, 11, 1, 7, 0))
    k = db.get(Supplement, 3)
    for back in range(1, 5):
        for s in (omega, d3, k):
            supl.take(db, s, DAY - timedelta(days=back))
    db.commit()
    assert supl.streak(db, user, DAY) == 4  # today still open
    supl.take(db, omega, DAY)
    assert supl.streak(db, user, DAY) == 4
    supl.take(db, d3, DAY)
    supl.take(db, k, DAY)
    assert supl.streak(db, user, DAY) == 5
    supl.pause(k)  # a paused one does not count
    supl.untake(db, k, DAY)
    assert supl.streak(db, user, DAY) == 5


# ---------------------------------------------------------------- pages and privacy


def test_page_add_tick_and_dashboard_card(auth_client, db, user):
    auth_client.post("/setup/zones")  # an empty household shows the welcome card, not the dashboard
    assert "＋ Dodaj suplement" in auth_client.get("/forma").text
    assert "Wypełnij jednym dotknięciem" in auth_client.get("/forma/suplementy/nowy").text
    r = auth_client.post(
        "/forma/suplementy",
        data={"name": "Kreatyna", "dose": "5 g", "time": "00:00", "per_take": "1", "pack_size": "100"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/forma"
    s = db.query(Supplement).one()
    assert (s.time, s.stock) == ("00:00", 100)

    page = auth_client.get("/forma").text
    assert f'href="/forma/suplementy/{s.id}"' in page and "zapas na 100 dni" in page
    assert "Kupiłem nowe opakowanie" in auth_client.get(f"/forma/suplementy/{s.id}").text
    assert "Suplementy dziś" in auth_client.get("/").text

    r = auth_client.post(f"/forma/suplementy/{s.id}/wziete", data={"where": "pulpit"}, headers={"HX-Request": "true"})
    assert "Wszystko wzięte" in r.text
    assert "Suplementy dziś" not in auth_client.get("/").text  # done for today – off the dashboard


def test_edit_and_buy(auth_client, db, user):
    s = supl.create(db, user, name="Omega", dose=None, time="08:00", pack_size=120, stock=10)
    db.commit()
    r = auth_client.post(
        f"/forma/suplementy/{s.id}",
        data={
            "name": "Omega-3",
            "dose": "1 kaps.",
            "time": "07:30",
            "per_take": "1",
            "pack_size": "120",
            "stock": "50",
            "restock_days": "7",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.refresh(s)
    assert (s.name, s.time, s.stock, s.restock_days) == ("Omega-3", "07:30", 50, 7)
    assert (
        auth_client.post(f"/forma/suplementy/{s.id}/kupione", data={"packs": "1", "price": "39,99"}).status_code == 200
    )
    db.refresh(s)
    assert s.stock == 170


def test_bad_input_comes_back_with_what_was_typed(auth_client, db):
    r = auth_client.post("/forma/suplementy", data={"name": "Magnez", "time": "25:00"})
    assert r.status_code == 422
    assert "Godzina w formacie 8:00" in r.text and 'value="Magnez"' in r.text
    assert db.query(Supplement).count() == 0


def test_someone_elses_supplement_is_invisible(auth_client, db, paula):
    s = supl.create(db, paula, name="Magnez", dose=None, time="21:00")
    db.commit()
    assert "Magnez" not in auth_client.get("/forma").text
    for path in ("wziete", "kupione", "wstrzymaj", "usun"):
        assert auth_client.post(f"/forma/suplementy/{s.id}/{path}").status_code == 404
    assert auth_client.get(f"/forma/suplementy/{s.id}").status_code == 404
    assert auth_client.post(f"/forma/suplementy/{s.id}", data={"name": "Hack"}).status_code == 404
    db.refresh(s)
    assert s.name == "Magnez" and s.active
    assert db.query(SupplementDose).count() == 0


def test_token_for_someone_else_ticks_nothing(client, db, user, paula):
    s = supl.create(db, paula, name="Magnez", dose=None, time="21:00")
    db.commit()
    tok = supl.token(user, DAY, [s])  # Jan's token naming Paula's supplement
    assert client.post(f"/n/supl/{tok}").text == "Nie ma już tych suplementów."
    assert db.query(SupplementDose).count() == 0
