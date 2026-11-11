from __future__ import annotations

from pathlib import Path

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
        assert row["pic"] in (0, 1) and 5 <= row["seconds"] <= 120
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
    assert st.routines(db) == [] and db.query(StretchRoutine).count() == 2


def test_pictures_and_duration(db):
    st.prepare(db)
    arm = db.query(Stretch).filter_by(key="arm_circles").one()  # pic: 1 – the start is a person standing
    assert st.image(arm) == st.image_end(arm) == "/static/exercises/arm-circles-1.webp"
    assert st.image_start(arm) == "/static/exercises/arm-circles.webp"
    cat = db.query(Stretch).filter_by(key="cat_cow").one()
    assert st.image(cat) == st.image_start(cat)
    r = st.for_kind(db, "after")
    work = sum((it.stretch.seconds + st.TRANSITION) * (2 if it.stretch.sided else 1) for it in r.items)
    assert st.total_seconds(r) == work
    assert st.fmt_duration(45) == "45 s" and st.fmt_duration(120) == "2 min" and st.fmt_duration(130) == "2 min 10 s"


def test_routines_on_the_phone(auth_client, db):
    page = auth_client.get("/forma/rozciaganie").text
    assert "Przed treningiem" in page and "Po treningu" in page
    assert "🤸 Rozciąganie" in auth_client.get("/forma").text

    # a new routine: add a stretch from the picker (static only), change its time, reorder, remove
    r = _post(auth_client, "/forma/rozciaganie", {"name": "Plecy po pracy"})
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    pick = auth_client.get(f"/forma/rozciaganie/{rid}/dodaj?typ=static").text
    assert "Pozycja dziecka" in pick and "Krążenia ramion" not in pick and "/forma/rozciaganie/cw/" in pick
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
    page = auth_client.get(f"/forma/rozciaganie/{rid}").text
    assert "Skręt tułowia na krześle" in page and "×2" in page

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
