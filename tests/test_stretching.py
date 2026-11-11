from __future__ import annotations

from pathlib import Path

from domownik import stretching as st
from domownik import training as tr
from domownik.models import Stretch, StretchRoutine

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
    sides = sum(2 if it.stretch.sided else 1 for it in r.items)
    work = sum(it.stretch.seconds * (2 if it.stretch.sided else 1) for it in r.items)
    assert st.total_seconds(r) == work + (sides - 1) * st.TRANSITION
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
