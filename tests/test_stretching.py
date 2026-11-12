from __future__ import annotations

from pathlib import Path

import pytest

from domownik import stretching as st
from domownik import training as tr
from domownik.models import Stretch, StretchRoutine, StretchSession, User
from domownik.security import hash_password

STATIC = Path(st.__file__).parent / "static"


def _post(c, url, data=None):
    return c.post(url, data=data or {}, follow_redirects=False)


def test_catalog_is_complete():
    """Every stretch has instructions, both pictures, a known type/area; the default routines use known keys."""
    cat = st.catalog()
    keys = [row["key"] for row in cat["stretches"]]
    assert len(keys) == len(set(keys))
    guides = tr.stretch_guides()
    assert not [k for k in keys if k not in guides], "brak instrukcji w exercise_guides.yaml (stretch)"
    for row in cat["stretches"]:
        assert row["type"] in cat["types"] and row["area"] in cat["areas"], row["key"]
        assert 5 <= row["seconds"] <= 120
        if "img" not in row:  # no good picture – the 🤸/🧘 icon everywhere
            assert "pic" not in row and "hold" not in row, row["key"]
            continue
        assert row["pic"] in (0, 1)
        slug = tr._slug(row["img"])
        assert (STATIC / "exercises" / f"{slug}.webp").is_file(), slug
        assert (STATIC / "exercises" / f"{slug}-1.webp").is_file(), slug
    for g in guides.values():
        assert 3 <= len(g.steps) <= 5 and 1 <= len(g.mistakes) <= 2
    for r in cat["routines"]:
        assert set(r["items"]) <= set(keys)


def test_seed_once_and_follow_catalog(db):
    st.prepare(db)
    db.commit()
    n = db.query(Stretch).count()
    assert n == len(st.catalog()["stretches"])
    before, after = st.for_kind(db, "before"), st.for_kind(db, "after")
    assert before.name == "Przed treningiem" and all(it.stretch.type == "dynamic" for it in before.items)
    assert after.name == "Po treningu" and all(it.stretch.type == "static" for it in after.items)
    # a renamed stretch follows the catalog; the routines are not made again after one is archived
    s = db.query(Stretch).filter_by(key="childs_pose").one()
    s.name = "stara nazwa"
    for r in st.routines(db):
        r.archived = True
    db.commit()
    st.prepare(db)
    db.commit()
    assert s.name == "Pozycja dziecka" and db.query(Stretch).count() == n
    seeds = len(st.catalog()["routines"])
    assert st.routines(db) == [] and db.query(StretchRoutine).count() == seeds


def test_routines_added_to_the_catalog_later(db):
    """A household with only the first two defaults gets the newer ones (at the end), once."""
    st.prepare(db)
    db.commit()
    for r in db.query(StretchRoutine).filter(StretchRoutine.seed.like("legs_%")):
        db.delete(r)
    db.commit()
    made = st.seed_routines(db)
    db.commit()
    assert [r.name for r in made] == ["Nogi – krótko", "Nogi – długo"]
    assert [r.name for r in st.routines(db)][-2:] == ["Nogi – krótko", "Nogi – długo"]
    assert st.seed_routines(db) == []
    short, long_ = made
    assert 5 * 60 <= st.total_seconds(short) <= 7 * 60
    assert 14 * 60 <= st.total_seconds(long_) <= 16 * 60
    assert all(it.stretch.area in {"hips", "glutes", "quads", "hamstrings", "groin", "calves"} for it in short.items)


def test_quick_start_puts_the_recent_first(db, user):
    st.prepare(db)
    db.commit()
    first = st.quick(db, user)
    assert len(first) == 3 and [r.position for r in first] == sorted(r.position for r in first)
    desk = db.query(StretchRoutine).filter_by(seed="desk").one()
    assert 3 * 60 <= st.total_seconds(desk) <= 6 * 60
    st.start(db, user, desk)
    db.commit()
    assert st.quick(db, user)[0] == desk
    assert st.fmt_minutes(395) == "7 min" and st.fmt_minutes(20) == "1 min"


def test_gear_labels_and_the_free_filter(auth_client, db):
    """Every gear key is known; the picker's "Bez sprzętu" hides the strap / stick / step / chair ones,
    keeps the other filters, and the routine says what to get ready."""
    cat = st.catalog()
    assert {row["gear"] for row in cat["stretches"] if row.get("gear")} <= set(cat["gear"])
    st.prepare(db)
    db.commit()
    strap = db.query(Stretch).filter_by(key="it_band_strap").one()
    assert st.gear(strap) == "pasek lub ręcznik" and st.gear(db.query(Stretch).filter_by(key="butterfly").one()) is None
    long_ = db.query(StretchRoutine).filter_by(seed="legs_long").one()
    assert st.routine_gear(long_) == ["stopień", "pasek lub ręcznik"]
    assert st.routine_gear(db.query(StretchRoutine).filter_by(seed="legs_short").one()) == []
    bedtime = db.query(StretchRoutine).filter_by(seed="bedtime").one()
    assert bedtime.name == "Przed snem" and st.routine_gear(bedtime) == []
    assert 5 * 60 <= st.total_seconds(bedtime) <= 8 * 60
    assert st.snapshot(long_)[3]["gear"] == "stopień"  # quad_step – the timer says "Przygotuj: stopień"

    rid = long_.id
    every = auth_client.get(f"/forma/rozciaganie/{rid}/dodaj?typ=static&partia=glutes").text
    assert "Bok uda i pośladek" in every and "🧰 pasek lub ręcznik" in every
    assert f"/forma/rozciaganie/{rid}/dodaj?typ=static&amp;partia=glutes&amp;sprzet=0" in every
    free = auth_client.get(f"/forma/rozciaganie/{rid}/dodaj?typ=static&partia=glutes&sprzet=0").text
    assert "Bok uda i pośladek" not in free and "czwórka" in free and "· 🧰" not in free
    assert f'/forma/rozciaganie/{rid}/dodaj?typ=static&amp;partia=glutes"' in free  # the chip turns it off
    # the search box filters in the browser over each stretch's name, body part, type and gear
    assert "data-pick-filter" in every and 'data-pick-text="Bok uda i pośladek' in every
    assert "Pośladki Statyczne pasek lub ręcznik" in every
    page = auth_client.get(f"/forma/rozciaganie/{rid}").text
    assert "🧰 Potrzebne: stopień, pasek lub ręcznik" in page
    lst = auth_client.get("/forma/rozciaganie").text
    assert "🧰 stopień, pasek lub ręcznik" in lst


def test_pictures_and_duration(db):
    st.prepare(db)
    arm = db.query(Stretch).filter_by(key="arm_circles").one()  # pic: 1 – the start is a person standing
    assert st.image(arm) == st.image_end(arm) == "/static/exercises/arm-circles-1.webp"
    assert st.image_start(arm) == "/static/exercises/arm-circles.webp"
    cat = db.query(Stretch).filter_by(key="cat_cow").one()
    assert st.image(cat) == st.image_start(cat)
    # the timer: the target position – the end frame, also for a dynamic one
    assert st.image_target(cat) == st.image_end(cat)
    four = db.query(Stretch).filter_by(key="figure_four").one()  # the icon is the start (lying)
    assert st.image(four) == st.image_start(four) and st.image_target(four) == st.image_end(four)
    chair = db.query(Stretch).filter_by(key="chair_side_bend").one()  # hold: 0 – the start is the bend
    assert st.image_target(chair) == st.image_start(chair)
    r = st.for_kind(db, "after")
    work = sum((it.stretch.seconds + st.TRANSITION) * (2 if it.stretch.sided else 1) for it in r.items)
    assert st.total_seconds(r) == work
    assert st.fmt_duration(45) == "45 s" and st.fmt_duration(120) == "2 min" and st.fmt_duration(130) == "2 min 10 s"


def test_routines_on_the_phone(auth_client, db):
    page = auth_client.get("/forma/rozciaganie").text
    assert "Przed treningiem" in page and "Po treningu" in page
    forma = auth_client.get("/forma").text
    assert "🤸 Rozciąganie" in forma and "Wszystkie zestawy" in forma and "/start" in forma

    # a new routine: add a stretch from the picker (static only), change its time, reorder, remove
    r = _post(auth_client, "/forma/rozciaganie", {"name": "Plecy po pracy"})
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    assert "_seconds" not in auth_client.get(f"/forma/rozciaganie/{rid}").text  # empty: straight to editing
    assert "Jeszcze pusto" in auth_client.get(f"/forma/rozciaganie/{rid}").text
    pick = auth_client.get(f"/forma/rozciaganie/{rid}/dodaj?typ=static").text
    assert "Pozycja dziecka" in pick and "Krążenia ramion" not in pick and "/forma/rozciaganie/cw/" in pick
    # the area chips: only one body part, combined with the type; an unknown area shows everything
    calves = auth_client.get(f"/forma/rozciaganie/{rid}/dodaj?typ=static&partia=calves").text
    assert "Łydka przy ścianie" in calves and "Pozycja dziecka" not in calves and "Krążenia stóp" not in calves
    assert "?typ=static&amp;partia=back" in calves or "?typ=static&partia=back" in calves
    assert "Pozycja dziecka" in auth_client.get(f"/forma/rozciaganie/{rid}/dodaj?partia=xyz").text
    child = db.query(Stretch).filter_by(key="childs_pose").one()
    twist = db.query(Stretch).filter_by(key="seated_twist").one()
    _post(auth_client, f"/forma/rozciaganie/{rid}/dodaj/{child.id}")
    _post(auth_client, f"/forma/rozciaganie/{rid}/dodaj/{twist.id}")
    routine = db.get(StretchRoutine, rid)
    db.refresh(routine)
    first, second = routine.items
    assert "✓ w zestawie" in auth_client.get(f"/forma/rozciaganie/{rid}/dodaj").text

    _post(
        auth_client,
        f"/forma/rozciaganie/{rid}",
        {"name": "Plecy", "kind": "after", f"i{first.id}_seconds": "60", f"i{second.id}_seconds": str(twist.seconds)},
    )
    db.refresh(routine)
    assert routine.name == "Plecy" and routine.kind == "after"
    assert first.seconds == 60 and second.seconds is None and second.duration == twist.seconds
    # the preview: no fields, "na stronę" for a sided stretch, ✏️ leads to the editing
    page = auth_client.get(f"/forma/rozciaganie/{rid}").text
    assert "Skręt tułowia na krześle" in page and "na stronę" in page and "_seconds" not in page
    assert f"/forma/rozciaganie/{rid}?edycja=1" in page and "Schowaj zestaw" not in page
    page = auth_client.get(f"/forma/rozciaganie/{rid}?edycja=1").text
    assert "×2" in page and "_seconds" in page and "Schowaj zestaw" in page

    _post(auth_client, f"/forma/rozciaganie/{rid}/cw/{second.id}/gora")
    db.refresh(routine)
    assert [it.stretch.key for it in routine.items] == ["seated_twist", "childs_pose"]
    _post(auth_client, f"/forma/rozciaganie/{rid}/cw/{second.id}/usun")
    db.refresh(routine)
    assert [it.stretch.key for it in routine.items] == ["childs_pose"]

    bad = _post(auth_client, f"/forma/rozciaganie/{rid}", {"kind": "after", f"i{first.id}_seconds": "2"})
    assert bad.status_code >= 400

    _post(auth_client, f"/forma/rozciaganie/{rid}/archiwum")
    assert auth_client.get(f"/forma/rozciaganie/{rid}").status_code == 404
    assert f'/forma/rozciaganie/{rid}"' not in auth_client.get("/forma/rozciaganie").text


def test_stretch_guide_modal_and_page(auth_client, db):
    st.prepare(db)
    db.commit()
    s = db.query(Stretch).filter_by(key="butterfly").one()
    modal = auth_client.get(f"/forma/rozciaganie/cw/{s.id}", headers={"HX-Request": "true", "HX-Target": "modal-body"})
    assert "Motyl" in modal.text and "Jak wykonać" in modal.text and "<html" not in modal.text
    page = auth_client.get(f"/forma/rozciaganie/cw/{s.id}").text
    assert "<html" in page and "Statyczne" in page and "Jak wykonać" in page
    assert auth_client.get("/forma/rozciaganie/cw/99999").status_code == 404
    # without a picture: the 🧘 icon, the instructions without the start/end frames
    cobra = db.query(Stretch).filter_by(key="cobra").one()
    assert st.image(cobra) is None and st.image_target(cobra) is None and st.icon(cobra) == "🧘"
    page = auth_client.get(f"/forma/rozciaganie/cw/{cobra.id}").text
    assert "Kobra" in page and "Jak wykonać" in page and "<figcaption>Start" not in page


def test_session_start_finish_and_history(auth_client, db, user):
    st.prepare(db)
    db.commit()
    before = st.for_kind(db, "before")
    r = _post(auth_client, f"/forma/rozciaganie/{before.id}/start")
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    s = db.get(StretchSession, sid)
    assert s.user_id == user.id and s.total == len(before.items) and s.finished_at is None
    assert s.steps[0] == {
        "id": before.items[0].stretch.id,
        "name": "Krążenia ramion",
        "seconds": 30,
        "sided": False,
        "img": "/static/exercises/arm-circles-1.webp",
        "target": "/static/exercises/arm-circles-1.webp",
        "gear": None,
    }
    page = auth_client.get(f"/forma/rozciaganie/sesja/{sid}").text
    assert "data-stretch-session" in page and "Kr\\u0105\\u017cenia ramion" in page and "▶ Start" in page

    # the routine edited during the session: the session keeps its own copy
    before.items[0].seconds = 90
    db.commit()
    db.refresh(s)
    assert s.steps[0]["seconds"] == 30

    # a second start drops the unfinished one
    r2 = _post(auth_client, f"/forma/rozciaganie/{before.id}/start")
    sid2 = int(r2.headers["location"].rsplit("/", 1)[1])
    db.expire_all()
    assert [x.id for x in db.query(StretchSession).all()] == [sid2]  # (SQLite may reuse the id)

    _post(auth_client, f"/forma/rozciaganie/sesja/{sid2}/koniec", {"done": "7", "seconds": "455"})
    s2 = db.get(StretchSession, sid2)
    db.refresh(s2)
    assert s2.finished_at is not None and s2.done == 7 and s2.seconds == 455
    summary = auth_client.get(f"/forma/rozciaganie/sesja/{sid2}").text
    assert f"7/{s2.total} ćwiczeń" in summary and "7 min 35 s" in summary and "data-stretch-session" not in summary
    # finishing twice keeps the first result; nonsense is clamped
    _post(auth_client, f"/forma/rozciaganie/sesja/{sid2}/koniec", {"done": "99", "seconds": "1"})
    db.refresh(s2)
    assert s2.done == 7
    forma = auth_client.get("/forma").text
    assert f"/forma/rozciaganie/sesja/{sid2}" in forma and "🤸 Przed treningiem" in forma

    _post(auth_client, f"/forma/rozciaganie/sesja/{sid2}/usun")
    db.expire_all()
    assert db.get(StretchSession, sid2) is None


def test_session_is_private_and_needs_stretches(auth_client, db):
    st.prepare(db)
    other = User(email="paula@example.com", name="Paula", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.flush()
    s = st.start(db, other, st.for_kind(db, "after"))
    db.commit()
    assert auth_client.get(f"/forma/rozciaganie/sesja/{s.id}").status_code == 404
    assert _post(auth_client, f"/forma/rozciaganie/sesja/{s.id}/koniec", {"done": "1"}).status_code == 404
    empty = _post(auth_client, "/forma/rozciaganie", {"name": "Pusty"})
    rid = int(empty.headers["location"].rsplit("/", 1)[1])
    assert _post(auth_client, f"/forma/rozciaganie/{rid}/start").status_code >= 400
    st.finish(s, done=99, seconds=-5)
    assert s.done == s.total and s.seconds == 0


def test_workout_offers_stretching_before_and_after(auth_client, db, user):
    plan = tr.plans(db)[0] if tr.plans(db) else None
    if plan is None:
        tr.ensure_library(db)
        tr.seed_plans(db)
        db.commit()
        plan = tr.plans(db)[0]
    r = _post(auth_client, "/forma/trening", {"plan_id": str(plan.id)})
    wid = int(r.headers["location"].rsplit("/", 1)[1])
    screen = auth_client.get(f"/forma/trening/{wid}").text
    before, after = st.for_kind(db, "before"), st.for_kind(db, "after")
    assert "🤸 Najpierw rozgrzewka" in screen and f"/forma/rozciaganie/{before.id}/start" in screen
    assert f'name="workout_id" value="{wid}"' in screen and "Rozciąganie po treningu" not in screen

    # the warm-up started from the workout: back to it while unfinished, then done
    r = _post(auth_client, f"/forma/rozciaganie/{before.id}/start", {"workout_id": str(wid)})
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    s = db.get(StretchSession, sid)
    assert s.workout_id == wid
    assert f"/forma/rozciaganie/sesja/{sid}" in auth_client.get(f"/forma/trening/{wid}").text
    assert f'href="/forma/trening/{wid}"' in auth_client.get(f"/forma/rozciaganie/sesja/{sid}").text
    _post(auth_client, f"/forma/rozciaganie/sesja/{sid}/koniec", {"done": str(s.total), "seconds": "300"})
    db.refresh(s)
    s.started_at = s.started_at.replace(year=s.started_at.year - 1)  # well before the workout ends
    db.commit()
    screen = auth_client.get(f"/forma/trening/{wid}").text
    assert "Najpierw rozgrzewka" not in screen and "🤸 Przed treningiem · ✓ · 5 min" in screen
    assert "Wróć do treningu" in auth_client.get(f"/forma/rozciaganie/sesja/{sid}").text

    # the workout done: stretching after it offered until one is done
    w = tr.get_own(db, user, wid)
    we = w.exercises[0]
    auth_client.post(
        f"/forma/trening/{wid}/cw/{we.id}/seria",
        data={"weight": "20", "reps": "10", "seconds": "30"},
        headers={"HX-Request": "true", "HX-Target": "workout"},
    )
    _post(auth_client, f"/forma/trening/{wid}/koniec")
    screen = auth_client.get(f"/forma/trening/{wid}").text
    assert "🧘 Rozciąganie po treningu?" in screen and f"/forma/rozciaganie/{after.id}/start" in screen
    r = _post(auth_client, f"/forma/rozciaganie/{after.id}/start", {"workout_id": str(wid)})
    sid2 = int(r.headers["location"].rsplit("/", 1)[1])
    _post(auth_client, f"/forma/rozciaganie/sesja/{sid2}/koniec", {"done": "3", "seconds": "120"})
    screen = auth_client.get(f"/forma/trening/{wid}").text
    assert "Rozciąganie po treningu?" not in screen and f"🤸 Po treningu · 3/{len(after.items)} · 2 min" in screen


def test_no_offer_for_an_old_workout_or_someone_elses(auth_client, db, user):
    tr.ensure_library(db)
    tr.seed_plans(db)
    db.commit()
    r = _post(auth_client, "/forma/trening", {"plan_id": "", "day": "2026-01-05"})
    wid = int(r.headers["location"].rsplit("/", 1)[1])
    assert "Najpierw rozgrzewka" not in auth_client.get(f"/forma/trening/{wid}").text
    other = User(email="paula@example.com", name="Paula", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.flush()
    w2 = tr.start(db, other, None)
    db.commit()
    st.prepare(db)
    db.commit()
    before = st.for_kind(db, "before")
    r = _post(auth_client, f"/forma/rozciaganie/{before.id}/start", {"workout_id": str(w2.id)})
    s = db.get(StretchSession, int(r.headers["location"].rsplit("/", 1)[1]))
    assert s.workout_id is None  # someone else's workout is not linked


def _paula(db):
    other = User(email="paula@example.com", name="Paula", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.commit()
    return other


def test_together_invite_join_and_shared_state(auth_client, db, user, monkeypatch):
    from domownik import together

    sent = []
    monkeypatch.setattr(together, "_send", lambda db, u, body: sent.append((u.id, body)) or 1)
    paula = _paula(db)
    st.prepare(db)
    db.commit()
    after = st.for_kind(db, "after")
    r = _post(auth_client, f"/forma/rozciaganie/{after.id}/start")
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    page = auth_client.get(f"/forma/rozciaganie/sesja/{sid}").text
    assert "👫 Rozciągaj się razem" in page and "data-group" not in page

    _post(auth_client, f"/forma/rozciaganie/sesja/{sid}/zapros", {"razem": str(paula.id)})
    s = db.get(StretchSession, sid)
    db.refresh(s)
    g = s.group
    assert g is not None and g.invited == [paula.id]
    assert sent == [(paula.id, sent[0][1])] and sent[0][1]["url"] == f"/forma/rozciaganie/razem/{g.id}"
    page = auth_client.get(f"/forma/rozciaganie/sesja/{sid}").text
    assert f'data-group="{g.id}"' in page and "Zakończ dla wszystkich" in page and "👫 Rozciągaj się razem" not in page

    # the shared state: a change from the current rev goes in, an outdated one gets the newer state back
    url = f"/forma/rozciaganie/razem/{g.id}/stan"
    d = auth_client.get(url).json()
    assert d["rev"] == 0 and d["state"] == {} and d["with"] == [] and d["waiting"] == 1
    state = {"idx": 1, "endsAt": 123, "left": 0, "started": True, "activeMs": 0, "runFrom": 100, "results": {}}
    d = auth_client.post(url, json={"rev": 0, "state": state | {"junk": 1}}).json()
    assert d["ok"] and d["rev"] == 1 and d["state"] == state
    d = auth_client.post(url, json={"rev": 0, "state": state | {"idx": 5}}).json()
    assert not d["ok"] and d["rev"] == 1 and d["state"]["idx"] == 1


def test_together_partner_joins(auth_client, db, user, monkeypatch):
    from domownik import together

    monkeypatch.setattr(together, "_send", lambda db, u, body: 1)
    paula = _paula(db)
    st.prepare(db)
    db.commit()
    host = st.start(db, user, st.for_kind(db, "after"))
    g = st.invite(db, host, [paula])
    db.commit()
    assert [x.id for x in st.open_invites(db, paula)] == [g.id]
    mine = st.join(db, g, paula)
    db.commit()
    assert mine.group_id == g.id and mine.steps == host.steps and mine.user_id == paula.id
    assert st.join(db, g, paula) is mine and st.open_invites(db, paula) == []
    d = auth_client.get(f"/forma/rozciaganie/razem/{g.id}/stan").json()
    assert d["with"] == ["Paula"] and d["waiting"] == 0

    # someone not invited: a 404 (and no join); after "end for everybody" nobody new comes in
    third = User(email="gosc@example.com", name="Gość", password_hash=hash_password("tajnehaslo3"))
    db.add(third)
    db.commit()
    with pytest.raises(ValueError):
        st.join(db, g, third)
    g.state = {"finished": True}
    db.commit()
    assert not st.is_live(g)


def test_together_forma_shows_invite(auth_client, db, user, monkeypatch):
    from domownik import together

    monkeypatch.setattr(together, "_send", lambda db, u, body: 1)
    paula = _paula(db)
    st.prepare(db)
    db.commit()
    host = st.start(db, paula, st.for_kind(db, "after"))
    g = st.invite(db, host, [user])
    db.commit()
    forma = auth_client.get("/forma").text
    assert f"/forma/rozciaganie/razem/{g.id}" in forma and "Dołącz do rozciągania: Paula" in forma
    r = auth_client.get(f"/forma/rozciaganie/razem/{g.id}", follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    s = db.get(StretchSession, sid)
    assert s.user_id == user.id and s.group_id == g.id
    page = auth_client.get(f"/forma/rozciaganie/sesja/{sid}").text
    assert f'data-group="{g.id}"' in page
    # the partner's own session stays private
    assert auth_client.get(f"/forma/rozciaganie/sesja/{host.id}").status_code == 404
