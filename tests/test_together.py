from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import pytest

from domownik import together, webpush
from domownik import training as tr
from domownik.models import Exercise, PushSubscription, User, Workout
from domownik.security import hash_password

D = date(2026, 11, 10)
T = datetime(2026, 11, 10, 18, 0)


@pytest.fixture
def sent(db, monkeypatch):
    out: list[tuple[int, dict]] = []
    monkeypatch.setattr(
        webpush, "deliver", lambda sub, data, *, urgent=False: out.append((sub.user_id, json.loads(data))) or True
    )
    return out


@pytest.fixture
def paula(db):
    u = User(email="paula@example.com", name="Paula", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def fbw(db):
    tr.ensure_library(db)
    ps = tr.seed_plans(db)
    db.commit()
    return ps


def _ex(db, key) -> Exercise:
    return db.query(Exercise).filter_by(key=key).one()


def _subscribe(db, u):
    db.add(PushSubscription(user_id=u.id, endpoint=f"https://push.example/{u.id}", p256dh="x", auth="y"))
    db.commit()


def _pair(db, user, paula, fbw):
    w = tr.start(db, user, fbw[0], at=T)
    db.commit()
    together.invite(db, w, [paula])
    pw = together.join(db, w.group, paula, today=D, at=T)
    db.commit()
    return w, pw


def test_invite_and_join(db, user, paula, fbw, sent):
    _subscribe(db, paula)
    w = tr.start(db, user, fbw[0], at=T)
    db.commit()
    g = together.invite(db, w, [paula])
    db.commit()
    assert sent[0][0] == paula.id and sent[0][1]["url"] == f"/forma/razem/{g.id}"
    assert "Jan zaczyna FBW 1" in sent[0][1]["title"]
    assert sent[0][1]["urgent"] is True  # Urgency: high – a phone asleep gets it at once
    assert together.open_invites(db, paula, D) == [(g, w)]
    assert together.open_invites(db, user, D) == []  # not to oneself

    pw = together.join(db, g, paula, today=D, at=T)
    db.commit()
    assert pw.user_id == paula.id and pw.plan_id == w.plan_id and pw.group_id == g.id
    assert [(x.exercise_id, x.slot, x.superset) for x in pw.exercises] == [
        (x.exercise_id, x.slot, x.superset) for x in w.exercises
    ]
    assert together.join(db, g, paula, today=D, at=T).id == pw.id  # the link opened twice
    assert together.open_invites(db, paula, D) == []
    assert together.waiting_for(db, w) == []


def test_not_invited_or_over(db, user, paula, fbw):
    w = tr.start(db, user, fbw[0], at=T)
    g = together.invite(db, w, [])
    with pytest.raises(ValueError, match="nie jest dla Ciebie"):
        together.join(db, g, paula, today=D, at=T)
    g.invited = [paula.id]
    tr.finish(db, w, at=T)
    with pytest.raises(ValueError, match="skończył"):
        together.join(db, g, paula, today=D, at=T)


def test_join_with_own_workout(db, user, paula, fbw):
    empty = tr.start(db, paula, fbw[1], at=T)
    db.commit()
    w = tr.start(db, user, fbw[0], at=T)
    g = together.invite(db, w, [paula])
    pw = together.join(db, g, paula, today=D, at=T)  # an empty start of her own is dropped
    db.commit()
    assert db.get(Workout, empty.id) is None and pw.group_id == g.id

    w2 = tr.start(db, user, fbw[1], at=T)
    g2 = together.invite(db, w2, [paula])
    tr.log_set(db, pw.exercises[0], weight=20, reps=10, at=T)
    with pytest.raises(together.Busy):
        together.join(db, g2, paula, today=D, at=T)
    pw2 = together.join(db, g2, paula, today=D, at=T, replace=True)
    assert pw.finished_at is not None and pw2.group_id == g2.id


def test_shared_changes(db, user, paula, fbw):
    w, pw = _pair(db, user, paula, fbw)
    # added – for both, the same slot
    we = together.add(db, w, _ex(db, "plank"))
    assert pw.exercises[-1].exercise.key == "plank" and pw.exercises[-1].slot == we.slot
    # swap for both
    first, pfirst = w.exercises[0], pw.exercises[0]
    lunge = _ex(db, "bulgarian")
    together.swap(db, w, first, lunge, both=True)
    assert pfirst.exercise_id == lunge.id
    # swap only for me: Paula keeps hers, the slot stays shared and she sees "other exercise"
    second, psecond = w.exercises[1], pw.exercises[1]
    before = psecond.exercise_id
    together.swap(db, w, second, _ex(db, "leg_press"), both=False)
    assert psecond.exercise_id == before
    v = together.views(w)[0]
    assert v.slots[second.slot].other_exercise and not v.slots[first.slot].other_exercise
    # removed – for her too, unless she already did sets of it
    tr.log_set(db, pfirst, weight=20, reps=10, at=T)
    together.remove(db, w, first)
    together.remove(db, w, w.exercises[-1])  # the plank
    db.commit()
    assert pfirst in pw.exercises and all(x.exercise.key != "plank" for x in pw.exercises)


def test_partner_sees_counts_only(db, user, paula, fbw):
    w, pw = _pair(db, user, paula, fbw)
    tr.log_set(db, pw.exercises[0], weight=33.5, reps=11, at=T)
    db.commit()
    v = together.views(w)[0]
    assert (v.name, v.done) == ("Paula", 1)
    assert v.slots[pw.exercises[0].slot].text == f"1/{pw.exercises[0].sets}"
    assert "33" not in repr(v) and "11" not in repr(v.slots)


# ---------------------------------------------------------------- over HTTP


def _login(client, email, password):
    from tests.conftest import csrf_of

    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    page = client.get("/login")
    client.post("/login", data={"email": email, "password": password, "csrf": csrf_of(page.text)})
    client.headers["X-CSRF-Token"] = csrf_of(client.get("/").text)


def test_http_flow(auth_client, db, user, paula, fbw, sent):
    _subscribe(db, paula)
    r = auth_client.post("/forma/trening", data={"plan_id": str(fbw[0].id)}, follow_redirects=False)
    assert r.status_code == 303
    w = db.query(Workout).filter_by(user_id=user.id).one()
    page = auth_client.get(f"/forma/trening/{w.id}").text
    assert "Zaproś do treningu" in page and f'name="razem" value="{paula.id}"' in page
    auth_client.post(f"/forma/trening/{w.id}/zapros", data={"razem": str(paula.id)})
    page = auth_client.get(f"/forma/trening/{w.id}").text
    assert "Czekam na: <b>Paula</b>" in page and "Zaproś do treningu" not in page
    url = sent[-1][1]["url"]

    # log a set as Jan before she joins – she must never see the weight
    we = w.exercises[0]
    auth_client.post(f"/forma/trening/{w.id}/cw/{we.id}/seria", data={"weight": "77,5", "reps": "9"})

    _login(auth_client, "paula@example.com", "tajnehaslo2")
    assert "Dołącz: Jan" in auth_client.get("/forma").text
    r = auth_client.get(url, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    pw = db.query(Workout).filter_by(user_id=paula.id).one()
    page = auth_client.get(f"/forma/trening/{pw.id}").text
    assert "Jan 1/" in page and "77,5" not in page and "77.5" not in page
    strip = auth_client.get(f"/forma/trening/{pw.id}/razem").text
    strip = re.sub(r'data-sig="[0-9a-f]+"', "", strip)  # a hash may contain "77" by chance
    assert "Jan" in strip and "77" not in strip
    other = auth_client.get(f"/forma/trening/{w.id}/razem")  # his workout is his: nothing, back to Forma
    assert other.text == "" and other.headers["HX-Redirect"] == "/forma"
    assert auth_client.get(f"/forma/trening/{w.id}").status_code == 404

    # her swap page offers "for both" / "only for me"
    pwe = pw.exercises[1]
    page = auth_client.get(f"/forma/trening/{pw.id}/cw/{pwe.id}/zamien").text
    assert "Zamiana dla:" in page and "?dla=oboje" in page
    lunge = _ex(db, "bulgarian")
    auth_client.post(f"/forma/trening/{pw.id}/cw/{pwe.id}/zamien/{lunge.id}?dla=ja")
    db.expire_all()
    assert w.exercises[1].exercise_id != lunge.id and pw.exercises[1].exercise_id == lunge.id


def test_superset_made_during_the_workout(db, user, paula, fbw, sent):
    """🔗 two added exercises into a superset – for the partner too; ✂ back; the order goes back and forth."""
    w, pw = _pair(db, user, paula, fbw)
    a = together.add(db, w, _ex(db, "rope_pulldown"))
    b = together.add(db, w, _ex(db, "rope_pushdown"))
    db.commit()
    assert a.superset is None and tr.following(w, a) is b and tr.following(w, b) is None
    old_sig = together.signature(pw)
    together.link_superset(db, w, a)
    db.commit()
    assert a.superset is not None and a.superset == b.superset
    assert a.superset not in {e.superset for e in w.exercises if e not in (a, b)}  # a new letter
    pa, pb = (together._same_slot(pw, x.slot) for x in (a, b))
    assert pa.superset is not None and pa.superset == pb.superset
    assert together.signature(pw) != old_sig  # the partner's screen reloads
    # back and forth: a → b, rest only after b
    tr.log_set(db, a, weight=50, reps=12, at=T)
    assert tr.next_up(w) is b and w.rest_until is None
    tr.log_set(db, b, weight=20, reps=12, at=T + timedelta(minutes=1))
    assert tr.next_up(w) is a and w.rest_until is not None
    # a third one joins the superset of its neighbour
    c = together.add(db, w, _ex(db, "plank"))
    together.link_superset(db, w, b)
    assert c.superset == a.superset
    with pytest.raises(ValueError):
        together.link_superset(db, w, c)  # the last one has no next
    together.unlink_superset(db, w, c)
    db.commit()
    assert a.superset is b.superset is c.superset is None
    assert pa.superset is None and pb.superset is None


def test_superset_buttons_on_the_screen(auth_client, db, user, fbw):
    w = tr.start(db, user, fbw[0], at=datetime.now())
    a = tr.add_exercise(db, w, _ex(db, "rope_pulldown"))
    b = tr.add_exercise(db, w, _ex(db, "rope_pushdown"))
    db.commit()
    hx = {"HX-Request": "true", "HX-Target": "workout"}
    page = auth_client.get(f"/forma/trening/{w.id}").text
    assert f'hx-post="/forma/trening/{w.id}/cw/{a.id}/superseria"' in page
    assert f'/cw/{b.id}/superseria"' not in page  # the last one
    r = auth_client.post(f"/forma/trening/{w.id}/cw/{a.id}/superseria", headers=hx)
    assert r.status_code == 200 and "Superseria" in r.text and f'/cw/{b.id}/rozlacz"' in r.text
    assert f'/cw/{a.id}/superseria"' not in r.text and f'/cw/{a.id}/rozlacz"' not in r.text
    r = auth_client.post(f"/forma/trening/{w.id}/cw/{b.id}/rozlacz", headers=hx)
    db.refresh(a)
    assert a.superset is None and f'/cw/{a.id}/superseria"' in r.text


def test_started_over_moves_the_partner(auth_client, db, user, paula, fbw, sent):
    """Jan deletes the training Paula joined and starts another: her empty one goes, the new invite is a tap away."""
    now_ = datetime.now().replace(microsecond=0)
    w = tr.start(db, user, fbw[0], at=now_)
    db.commit()
    together.invite(db, w, [paula])
    pw = together.join(db, w.group, paula, today=now_.date(), at=now_)
    db.commit()
    pw_id = pw.id
    auth_client.post(f"/forma/trening/{w.id}/usun")
    db.expire_all()
    assert db.get(Workout, pw_id) is None  # nothing done in it – gone with his

    # she has a set in hers: kept when he deletes his
    w2 = tr.start(db, user, fbw[0], at=now_)
    db.commit()
    together.invite(db, w2, [paula])
    pw2 = together.join(db, w2.group, paula, today=now_.date(), at=now_)
    tr.log_set(db, pw2.exercises[0], weight=10, reps=10, at=now_)
    db.commit()
    pw2_id = pw2.id
    auth_client.post(f"/forma/trening/{w2.id}/usun")
    db.expire_all()
    assert db.get(Workout, pw2_id) is not None

    # he starts over in a third one and invites her: her screen offers to move over
    w3 = tr.start(db, user, fbw[1], at=now_)
    db.commit()
    g3 = together.invite(db, w3, [paula])
    db.commit()
    _login(auth_client, "paula@example.com", "tajnehaslo2")
    page = auth_client.get(f"/forma/trening/{pw2.id}").text
    assert "<b>Jan</b> zaprasza do treningu: FBW 2" in page and f'href="/forma/razem/{g3.id}"' in page
    assert f'hx-get="/forma/trening/{pw2.id}/razem"' in page  # polled – the offer comes without a reload
    gone = auth_client.get("/forma/trening/99999/razem")  # her screen of a removed one: back to Forma
    assert gone.headers["HX-Redirect"] == "/forma"
    r = auth_client.get(f"/forma/razem/{g3.id}", follow_redirects=False)
    assert "Masz już swój trening w toku" in r.text  # hers has a set: asked first
    r = auth_client.post(f"/forma/razem/{g3.id}/dolacz", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert together.member(db.get(type(g3), g3.id), paula) is not None
