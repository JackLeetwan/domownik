"""📏 Wymiary: entries keep the newest value of each measurement, Sylwetka fills in for the owner,
🔒 private entries stay hidden, brand sizes and the copied text, children's foot reminder."""

from __future__ import annotations

from datetime import timedelta

import pytest

from domownik import sizes
from domownik.config import today
from domownik.models import BodyCheck, BodySize, BodyWeight, FitSize, Item, User
from domownik.security import hash_password
from tests.conftest import csrf_of


@pytest.fixture
def family(db, user):
    me = Item(name="Jan", kind="member", user_id=user.id)
    kid = Item(name="Ala", kind="child")
    db.add_all([me, kid])
    db.commit()
    return {"me": me, "kid": kid}


def _csrf(auth_client, item_id: int) -> str:
    return csrf_of(auth_client.get(f"/zdrowie/{item_id}").text)


def test_parse_and_shoe_size():
    assert sizes.parse_cm("27,5 cm") == 27.5
    assert sizes.parse_cm("abc") is None and sizes.parse_cm("0") is None and sizes.parse_cm("") is None
    assert sizes.eu_shoe(27.5) == "43,5"
    assert sizes.eu_shoe(None) is None and sizes.eu_shoe(50) is None


def test_measure_keeps_newest_value_per_field(auth_client, db, family):
    me = family["me"]
    csrf = _csrf(auth_client, me.id)
    old = (today() - timedelta(days=30)).isoformat()
    r = auth_client.post(
        f"/zdrowie/{me.id}/wymiary",
        data={"csrf": csrf, "measured_on": old, "height": "182", "waist": "86", "foot_len": "27,5"},
    )
    assert r.status_code in (200, 303)
    auth_client.post(f"/zdrowie/{me.id}/wymiary", data={"csrf": csrf, "waist": "84"})
    assert auth_client.post(f"/zdrowie/{me.id}/wymiary", data={"csrf": csrf, "note": "nic"}).status_code == 400
    assert db.query(BodySize).count() == 2
    html = auth_client.get(f"/zdrowie/{me.id}").text
    assert "📏 Wymiary" in html
    assert "182 cm" in html and "84 cm" in html and "-2 od" in html
    assert "≈ EU 43,5" in html


def test_sylwetka_and_weight_only_for_owner(client, auth_client, db, user, family):
    me = family["me"]
    db.add_all(
        [
            BodySize(item_id=me.id, measured_on=today() - timedelta(days=60), chest=100),
            BodyCheck(user_id=user.id, day=today() - timedelta(days=3), chest=104, waist=83),
            BodyWeight(user_id=user.id, day=today(), kg=78.4),
        ]
    )
    db.commit()
    s = sizes.load(db, me, user)
    assert s.values["chest"].value == 104 and s.values["chest"].source == "Sylwetka"
    assert "waga 78,4 kg" in s.copy_text()

    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.commit()
    s2 = sizes.load(db, me, other)
    assert s2.values["chest"].value == 100 and "waist" not in s2.values and s2.weight is None


def test_private_entry_hidden_from_others(client, db, user, family):
    me = family["me"]
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.commit()
    db.add_all(
        [
            BodySize(item_id=me.id, measured_on=today(), height=182),
            BodySize(item_id=me.id, measured_on=today(), hips=987, private_to_id=user.id),
        ]
    )
    db.commit()
    page = client.get("/login")
    client.post("/login", data={"email": "ola@example.com", "password": "tajnehaslo2", "csrf": csrf_of(page.text)})
    html = client.get(f"/zdrowie/{me.id}").text
    assert "182 cm" in html and "987" not in html
    hidden = db.query(BodySize).filter(BodySize.hips == 987).one()
    csrf = csrf_of(html)
    assert client.post(f"/zdrowie/wymiary/{hidden.id}/delete", data={"csrf": csrf}).status_code == 404


def test_fit_sizes_and_copy(auth_client, db, family):
    me = family["me"]
    csrf = _csrf(auth_client, me.id)
    auth_client.post(
        f"/zdrowie/{me.id}/rozmiary", data={"csrf": csrf, "what": "Jeansy", "brand": "Levi's 501", "size": "32/32"}
    )
    auth_client.post(f"/zdrowie/{me.id}/wymiary", data={"csrf": csrf, "foot_len": "27,5", "foot_width": "10"})
    assert auth_client.post(f"/zdrowie/{me.id}/rozmiary", data={"csrf": csrf, "what": "Buty"}).status_code == 400
    html = auth_client.get(f"/zdrowie/{me.id}").text
    assert "Levi&#39;s 501" in html and "📋 Kopiuj wymiary" in html
    text = sizes.load(db, me, db.get(User, me.user_id)).copy_text()
    assert "Jeansy Levi's 501: 32/32" in text and "stopa 27,5 cm (szerokość 10) ≈ EU 43,5" in text
    fit = db.query(FitSize).one()
    auth_client.post(f"/zdrowie/rozmiary/{fit.id}/delete", data={"csrf": csrf})
    assert db.query(FitSize).count() == 0


def test_child_foot_reminder(auth_client, db, family):
    kid = family["kid"]
    assert "Zmierz stopę" in auth_client.get("/zdrowie").text
    db.add(BodySize(item_id=kid.id, measured_on=today() - timedelta(days=120), foot_len=15))
    db.commit()
    assert "4 mies. temu" in auth_client.get("/zdrowie").text
    db.add(BodySize(item_id=kid.id, measured_on=today(), foot_len=15.8))
    db.commit()
    html = auth_client.get(f"/zdrowie/{kid.id}").text
    assert "mies. temu" not in html and "+0,8 od" in html


def test_every_measurement_listed_with_its_guide(auth_client, family):
    me = family["me"]
    html = auth_client.get(f"/zdrowie/{me.id}").text
    for k, lbl, *_ in sizes.FIELDS:
        assert f"Jak mierzyć: {lbl}" in html  # the ⓘ of each one, measured or not
        assert sizes.GUIDE[k][1][0] in html
    assert html.count('form="sz-quick" inputmode') == len(sizes.FIELDS)
    assert html.count('class="szf"') == len(sizes.FIELDS)


def test_limb_girths_and_outer_leg(auth_client, db, family):
    me = family["me"]
    csrf = _csrf(auth_client, me.id)
    data = {"csrf": csrf, "biceps": "37", "forearm": "31,5", "thigh": "53", "calf": "38", "outseam": "105"}
    assert auth_client.post(f"/zdrowie/{me.id}/wymiary", data=data).status_code in (200, 303)
    e = db.query(BodySize).one()
    assert (e.biceps, e.forearm, e.thigh, e.calf, e.outseam) == (37, 31.5, 53, 38, 105)
    html = auth_client.get(f"/zdrowie/{me.id}").text
    assert "31,5 cm" in html and html.count('form="sz-quick" inputmode') == len(sizes.FIELDS) - 5
    assert "biceps 37" in sizes.load(db, me, db.get(User, me.user_id)).copy_text()


def test_edit_measurement(auth_client, db, user, family):
    me = family["me"]
    e = BodySize(item_id=me.id, measured_on=today(), height=184, chest=104, note="stara")
    db.add(e)
    db.commit()
    csrf = _csrf(auth_client, me.id)
    assert "✏️ Popraw" in auth_client.get(f"/zdrowie/{me.id}").text
    day = (today() - timedelta(days=5)).isoformat()
    r = auth_client.post(
        f"/zdrowie/wymiary/{e.id}/edit",
        data={
            "csrf": csrf,
            "height": "183,5",
            "chest": "",
            "shoulders": "47",
            "measured_on": day,
            "note": "",
            "private_field": "1",
            "private": "1",
        },
    )
    assert r.status_code in (200, 303)
    db.refresh(e)
    assert (e.height, e.chest, e.shoulders, e.note) == (183.5, None, 47, None)
    assert e.measured_on.isoformat() == day and e.private_to_id == user.id
    assert auth_client.post(f"/zdrowie/wymiary/{e.id}/edit", data={"csrf": csrf, "height": ""}).status_code == 400


def test_private_entry_not_editable_by_others(client, db, user, family):
    me = family["me"]
    hidden = BodySize(item_id=me.id, measured_on=today(), height=184, private_to_id=user.id)
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add_all([hidden, other])
    db.commit()
    page = client.get("/login")
    client.post("/login", data={"email": "ola@example.com", "password": "tajnehaslo2", "csrf": csrf_of(page.text)})
    csrf = csrf_of(client.get(f"/zdrowie/{me.id}").text)
    assert client.post(f"/zdrowie/wymiary/{hidden.id}/edit", data={"csrf": csrf, "height": "1"}).status_code == 404
    db.refresh(hidden)
    assert hidden.height == 184


def test_missing_typed_straight_into_the_rows(auth_client, db, user, family):
    me = family["me"]
    db.add(BodySize(item_id=me.id, measured_on=today() - timedelta(days=9), height=184, private_to_id=user.id))
    db.commit()
    html = auth_client.get(f"/zdrowie/{me.id}").text
    assert 'id="sz-quick"' in html and 'name="private" value="1"' in html.split('id="sz-quick"')[1].split("</form>")[0]
    assert 'name="height" form="sz-quick"' not in html  # measured already – no quick field
    csrf = csrf_of(html)
    data = {"csrf": csrf, "shoulders": "47", "sleeve": "64,5", "private": "1"}
    assert auth_client.post(f"/zdrowie/{me.id}/wymiary", data=data).status_code in (200, 303)
    e = db.query(BodySize).order_by(BodySize.id.desc()).first()
    assert (e.shoulders, e.sleeve, e.measured_on, e.private_to_id) == (47, 64.5, today(), user.id)
