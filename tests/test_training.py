from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from domownik import training as tr
from domownik import webpush
from domownik.models import Exercise, PushSubscription, User, Workout
from domownik.security import hash_password

D = date(2026, 11, 10)
T = datetime(2026, 11, 10, 18, 0)


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(webpush, "deliver", lambda sub, data, *, urgent=False: out.append(json.loads(data)) or True)
    return out


@pytest.fixture
def paula(db):
    u = User(email="paula@example.com", name="Paula", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def fbw(db):
    ps = tr.seed_plans(db)
    db.commit()
    return ps


def _ex(db, key) -> Exercise:
    return db.query(Exercise).filter_by(key=key).one()


def _do(db, w, rows: dict[str, list[tuple[float | None, int]]], at=T):
    by_key = {we.exercise.key: we for we in w.exercises}
    for key, sets in rows.items():
        for weight, reps in sets:
            tr.log_set(db, by_key[key], weight=weight, reps=reps, at=at)
    db.flush()


def test_seed_and_range(db, fbw):
    assert [p.name for p in fbw] == ["FBW 1", "FBW 2", "FBW 3", "Superserie we dwoje"]
    item = fbw[0].items[2]
    assert item.exercise.name == "Wyciskanie hantli na ławce płaskiej"
    assert (item.sets, item.reps_min, item.reps_max) == (4, 8, 12)
    assert tr.range_for(16) == (12, 16) and tr.range_for(20) == (15, 20)
    assert [i.superset for i in fbw[0].items] == [1, 1, None, None, 2, 2, None]
    assert tr.seed_plans(db) == []  # only once


def test_duo_plan_added_later(db, user):
    """A household with plans already gets only the later ones (once); the duo plan stays out of rotation."""
    from domownik.models import WorkoutPlan

    ps = tr.seed_plans(db)
    duo = ps.pop()
    db.delete(duo)
    for p in ps:  # as made before the seed column: no seed, one renamed
        p.seed = None
    ps[0].name = "FBW A"
    db.flush()
    (again,) = tr.seed_plans(db)  # FBW not remade, only the duo plan
    assert again.name == "Superserie we dwoje" and again.position == 3
    assert [i.superset for i in again.items] == [1, 1, 2, 2, 3, 3, 4, 4]
    assert again.items[-1].rest_seconds == 60
    assert tr.seed_plans(db) == []
    assert db.query(WorkoutPlan).count() == 4
    # rotation: FBW 1 → duo → still FBW 2 next
    tr.start(db, user, ps[0], at=T - timedelta(days=2))
    tr.start(db, user, again, at=T - timedelta(days=1))
    assert tr.next_plan(db, user) == ps[1]


def test_rotation(db, user, fbw):
    assert tr.next_plan(db, user).name == "FBW 1"
    w = tr.start(db, user, fbw[0], at=T - timedelta(days=2))
    tr.finish(db, w, T - timedelta(days=2))
    assert tr.next_plan(db, user).name == "FBW 2"
    tr.start(db, user, fbw[2], at=T)
    assert tr.next_plan(db, user).name == "FBW 1"  # after 3 → 1


def test_double_progression(db, user, fbw):
    w1 = tr.start(db, user, fbw[0], at=T - timedelta(days=7))
    _do(db, w1, {"db_bench": [(20, 12), (20, 12), (20, 11), (20, 10)]}, at=T - timedelta(days=7))
    tr.finish(db, w1, T - timedelta(days=7))

    w2 = tr.start(db, user, fbw[0], at=T)
    bench = next(we for we in w2.exercises if we.exercise.key == "db_bench")
    s = tr.suggest(db, user, bench)
    assert (s.weight, s.reps, s.up) == (20, 11, False)  # same weight, one more rep than the worst set
    assert s.last == "20 kg × 12, 12, 11, 10"

    _do(db, w2, {"db_bench": [(20, 12)] * 4})
    tr.finish(db, w2, T)
    w3 = tr.start(db, user, fbw[0], at=T + timedelta(days=7))
    bench = next(we for we in w3.exercises if we.exercise.key == "db_bench")
    s = tr.suggest(db, user, bench)
    assert (s.weight, s.reps, s.up, s.note) == (22, 8, True, "+2 kg")


def test_bodyweight_and_time(db, user, fbw):
    w1 = tr.start(db, user, fbw[1], at=T - timedelta(days=3))
    plank = next(we for we in w1.exercises if we.exercise.key == "plank")
    for _ in range(4):
        tr.log_set(db, plank, weight=None, reps=None, seconds=45, at=T - timedelta(days=3))
    tr.finish(db, w1, T - timedelta(days=3))
    w2 = tr.start(db, user, fbw[1], at=T)
    plank = next(we for we in w2.exercises if we.exercise.key == "plank")
    assert tr.suggest(db, user, plank).seconds == 50


def test_substitutes_and_away(db, user, fbw):
    tr.ensure_library(db)
    same, muscle = tr.substitutes(db, _ex(db, "bench_press"))
    assert "db_bench" in {e.key for e in same} and "cable_fly" in {e.key for e in muscle}
    same, _ = tr.substitutes(db, _ex(db, "bench_press"), set(tr.away_equipment()))
    assert {e.equipment for e in same} <= {"bodyweight", "band", "bar"} and "push_up" in {e.key for e in same}

    w = tr.start(db, user, fbw[0], away=True, at=T)
    keys = [we.exercise.key for we in w.exercises]
    assert keys[0] == "spanish_squat" and keys[1] == "pull_up"  # band and bar are at hand
    bench = w.exercises[2]
    assert bench.exercise.equipment in tr.away_equipment() and bench.swapped_from.key == "db_bench"


def test_swap_only_without_sets(db, user, fbw):
    w = tr.start(db, user, fbw[0], at=T)
    we = w.exercises[2]
    tr.swap(db, we, _ex(db, "machine_chest_press"))
    assert we.exercise.key == "machine_chest_press" and we.swapped_from.key == "db_bench"
    tr.log_set(db, we, weight=40, reps=10, at=T)
    with pytest.raises(ValueError):
        tr.swap(db, we, _ex(db, "bench_press"))


def test_rest_and_superset(db, user, fbw):
    w = tr.start(db, user, fbw[0], at=T)
    squat, pull = w.exercises[0], w.exercises[1]
    tr.log_set(db, squat, weight=None, reps=20, at=T)
    assert w.rest_until is None  # straight to the partner of the superset
    tr.log_set(db, pull, weight=None, reps=10, at=T)
    assert w.rest_until == T + timedelta(seconds=90)
    # −30 / +30 s: the current rest moves and the exercise keeps the new length (the plan does not change)
    w.rest_pushed = True
    tr.adjust_rest(db, w, 30, at=T + timedelta(seconds=10))
    assert w.rest_until == T + timedelta(seconds=120) and pull.rest_seconds == 120 and not w.rest_pushed
    tr.adjust_rest(db, w, -30, at=T + timedelta(seconds=10))
    tr.adjust_rest(db, w, -30, at=T + timedelta(seconds=10))
    assert w.rest_until == T + timedelta(seconds=60) and pull.rest_seconds == 60
    tr.adjust_rest(db, w, -30, at=T + timedelta(seconds=50))  # never into the past: the rest is over now
    assert w.rest_until == T + timedelta(seconds=50)
    tr.adjust_rest(db, w, 30, at=T + timedelta(seconds=70))  # ran out → +30 s from now
    assert w.rest_until == T + timedelta(seconds=100) and pull.rest_seconds == 60
    assert all(it.rest_seconds == 90 for it in fbw[0].items if it.superset)
    backfill = tr.start(db, user, fbw[1], day=D - timedelta(days=1), at=T)
    tr.log_set(db, backfill.exercises[0], weight=20, reps=12, at=T)
    assert backfill.rest_until is None  # an old day typed in later has no rest


def test_rest_push(db, user, fbw, sent):
    db.add(PushSubscription(user_id=user.id, endpoint="https://push.example.com/x", p256dh="pk", auth="au"))
    w = tr.start(db, user, fbw[0], at=T)
    tr.log_set(db, w.exercises[2], weight=20, reps=12, at=T)
    db.commit()
    assert tr.run_rest(db, T + timedelta(seconds=89)) == 0
    assert tr.run_rest(db, T + timedelta(seconds=90)) == 1
    assert sent[0]["title"] == "⏱ Koniec przerwy"
    assert sent[0]["body"] == "Wyciskanie hantli na ławce płaskiej – seria 2/4"
    assert tr.run_rest(db, T + timedelta(seconds=120)) == 0


def test_week_counts_days_not_sessions(db, user, fbw):
    # Jefit-style: four one-exercise sessions on one day are one training
    for i in range(4):
        w = tr.start(db, user, None, at=T - timedelta(days=1, minutes=-10 * i))
        tr.add_exercise(db, w, _ex(db, "db_bench"))
        tr.log_set(db, w.exercises[0], weight=20, reps=10, at=T - timedelta(days=1))
        tr.finish(db, w, T - timedelta(days=1))
    wk = tr.week(db, user, D)
    assert (wk.done, wk.goal, wk.days_since) == (1, 3, 1)


def test_nudge(db, user, fbw, sent):
    db.add(PushSubscription(user_id=user.id, endpoint="https://push.example.com/x", p256dh="pk", auth="au"))
    w = tr.start(db, user, fbw[0], at=T - timedelta(days=5))
    tr.log_set(db, w.exercises[2], weight=20, reps=12, at=T - timedelta(days=5))
    tr.finish(db, w, T - timedelta(days=5))
    tr.prefs(db, user)
    db.commit()
    nudge = datetime(2026, 11, 10, tr.NUDGE_HOUR, 0)
    assert tr.run_nudge(db, nudge - timedelta(hours=1)) == 0
    assert tr.run_nudge(db, nudge) == 1
    assert sent[0]["title"] == "💪 5 dni bez treningu" and "Dziś FBW 2?" in sent[0]["body"]
    assert tr.run_nudge(db, nudge + timedelta(minutes=5)) == 0
    assert tr.run_nudge(db, nudge + timedelta(days=1)) == 0  # quiet for gap_days


def test_finish_drops_untouched(db, user, fbw):
    w = tr.start(db, user, fbw[0], at=T)
    tr.log_set(db, w.exercises[2], weight=20, reps=12, at=T)
    tr.finish(db, w, T)
    assert [we.exercise.key for we in w.exercises] == ["db_bench"]
    assert db.get(Workout, w.id).finished_at == T


# ---------------------------------------------------------------- pages


def _post(c, url, data=None):
    return c.post(url, data=data or {}, follow_redirects=False)


def test_full_flow_on_the_phone(auth_client, db, user):
    page = auth_client.get("/forma").text
    assert "▶ Zacznij FBW 1" in page and "0/3" in page
    plan = db.query(Workout).count() == 0 and tr.plans(db)[0]
    r = _post(auth_client, "/forma/trening", {"plan_id": str(plan.id)})
    wid = int(r.headers["location"].rsplit("/", 1)[1])
    w = db.get(Workout, wid)
    bench = next(we for we in w.exercises if we.exercise.key == "db_bench")

    screen = auth_client.get(f"/forma/trening/{wid}").text
    assert "Spanish squat z gumą" in screen and "Superseria A" in screen and "⇄ Zajęte? Zamień" in screen
    # the picture: start and end side by side – the whole movement at a glance
    assert 'class="ex-pic  both"' in screen
    assert "/static/exercises/pullups.webp" in screen and "/static/exercises/pullups-1.webp" in screen

    r = auth_client.post(
        f"/forma/trening/{wid}/cw/{bench.id}/seria",
        data={"weight": "22,5", "reps": "10"},
        headers={"HX-Request": "true", "HX-Target": "workout"},
    )
    assert r.status_code == 200 and r.text.lstrip().startswith('<div id="workout"')
    assert "22,5 kg × 10" in r.text and "data-rest-ends" in r.text
    assert f'hx-post="/forma/trening/{wid}/przerwa?o=30"' in r.text and "−30 s" in r.text
    rest = bench.rest_seconds
    hx = {"HX-Request": "true", "HX-Target": "workout"}
    r = auth_client.post(f"/forma/trening/{wid}/przerwa?o=30", headers=hx)
    assert r.status_code == 200 and f"przerwa {rest + 30} s" in r.text
    auth_client.post(f"/forma/trening/{wid}/przerwa?o=500", headers=hx)  # only ±30
    db.refresh(bench)
    assert bench.rest_seconds == rest + 30
    # the plan's sets done: "＋ jeszcze jedna seria" opens the usual form – its own weight and reps
    for _ in range(bench.sets - len(bench.done)):
        tr.log_set(db, bench, weight=25, reps=8)
    db.commit()
    screen = auth_client.get(f"/forma/trening/{wid}").text
    more = screen.split(f'id="we{bench.id}"', 1)[1].split("</article>", 1)[0]
    assert '<details class="wo-more"' in more and "＋ jeszcze jedna seria" in more
    assert 'name="weight" inputmode="decimal" value="25"' in more and f"✓ Seria {bench.sets + 1}<" in more
    assert "&lt;form" not in more  # the folded form is markup, not text
    for s in bench.done[-(bench.sets - 1):]:
        tr.undo_set(db, s)
    db.commit()
    r = auth_client.post(
        f"/forma/trening/{wid}/cw/{bench.id}/seria",
        data={"weight": "22,5", "reps": ""},
        headers={"HX-Request": "true", "HX-Target": "workout"},
    )
    assert "Wpisz liczbę powtórzeń" in r.text

    # a busy machine: substitutes of the shoulder press, then the swap
    press = next(we for we in w.exercises if we.exercise.key == "db_shoulder_press")
    pick = auth_client.get(f"/forma/trening/{wid}/cw/{press.id}/zamien").text
    assert "Ten sam ruch" in pick and "Wyciskanie żołnierskie" in pick
    assert (
        f'hx-get="/forma/cwiczenie/{press.exercise_id}"' in auth_client.get(f"/forma/trening/{wid}").text
    )  # ⓘ = the picture
    assert 'class="ex-info"' in pick
    ohp = db.query(Exercise).filter_by(key="ohp").one()
    _post(auth_client, f"/forma/trening/{wid}/cw/{press.id}/zamien/{ohp.id}")
    db.refresh(press)
    assert press.exercise.key == "ohp"

    core = auth_client.get(f"/forma/trening/{wid}/dodaj?partia=core").text
    assert "Dodaj ćwiczenie" in core
    # the search box filters in the browser over name, group, equipment, body part and the Jefit names
    assert "data-pick-filter" in core and "data-pick-none" in core and "wszystko=1" in core
    assert 'data-pick-text="Spięcia odwrotne Brzuch – spięcia Masa ciała Brzuch Reverse Crunch"' in core
    assert "data-pick-filter" in pick
    _post(auth_client, f"/forma/trening/{wid}/koniec")
    db.refresh(w)
    assert w.finished_at is not None and len(w.exercises) == 1
    page = auth_client.get("/forma").text
    assert "▶ Zacznij FBW 2" in page and "1/3" in page and "Ostatni: dziś" in page
    assert "22,5 kg × 10" in auth_client.get(f"/forma/postepy/{bench.exercise_id}").text
    assert "Wyciskanie hantli na ławce płaskiej" in auth_client.get("/forma/postepy").text


def test_backfill_and_empty_finish(auth_client, db, user):
    r = _post(auth_client, "/forma/trening", {"plan_id": "", "day": "2026-11-09"})
    wid = int(r.headers["location"].rsplit("/", 1)[1])
    assert db.get(Workout, wid).day == date(2026, 11, 9)
    _post(auth_client, f"/forma/trening/{wid}/koniec")  # nothing done → gone
    assert db.get(Workout, wid) is None
    assert _post(auth_client, "/forma/trening", {"day": "2999-01-01"}).status_code == 400


def test_workouts_are_private(auth_client, db, paula, fbw):
    w = tr.start(db, paula, fbw[0], at=T)
    tr.log_set(db, w.exercises[2], weight=30, reps=12, at=T)
    db.commit()
    assert auth_client.get(f"/forma/trening/{w.id}").status_code == 404
    we = w.exercises[2]
    assert _post(auth_client, f"/forma/trening/{w.id}/cw/{we.id}/seria", {"reps": "5"}).status_code == 404
    assert _post(auth_client, f"/forma/trening/{w.id}/usun").status_code == 404
    assert "30 kg" not in auth_client.get(f"/forma/postepy/{we.exercise_id}").text
    assert "0/3" in auth_client.get("/forma").text


def test_plan_editing(auth_client, db, fbw):
    p = fbw[0]
    it = p.items[2]
    data = {"name": "FBW A"}
    for x in p.items:
        k = f"i{x.id}_"
        data |= {k + "sets": str(x.sets), k + "rest": str(x.rest_seconds), k + "superset": str(x.superset or "")}
        data |= {
            k + "reps_min": str(x.reps_min or ""),
            k + "reps_max": str(x.reps_max or ""),
            k + "seconds": str(x.seconds or ""),
        }
    data[f"i{it.id}_reps_min"], data[f"i{it.id}_reps_max"] = "6", "10"
    _post(auth_client, f"/forma/plany/{p.id}", data)
    db.refresh(p)
    db.refresh(it)
    assert p.name == "FBW A" and (it.reps_min, it.reps_max) == (6, 10)
    _post(auth_client, f"/forma/plany/{p.id}/cw/{it.id}/gora")
    db.refresh(p)
    assert p.items[1].id == it.id
    plank = db.query(Exercise).filter_by(key="plank").one()
    _post(auth_client, f"/forma/plany/{p.id}/dodaj/{plank.id}")
    db.refresh(p)
    assert p.items[-1].exercise_id == plank.id and p.items[-1].seconds == 30
    _post(auth_client, "/forma/cel", {"weekly_goal": "2", "gap_days": "4"})
    assert "0/2" in auth_client.get("/forma").text


def test_superset_goes_back_and_forth(db, user, fbw):
    w = tr.start(db, user, fbw[0], at=T)
    squat, pull, bench = w.exercises[0], w.exercises[1], w.exercises[2]
    assert tr.next_up(w) is squat
    tr.log_set(db, squat, weight=None, reps=20, at=T)
    assert tr.next_up(w) is pull
    tr.log_set(db, pull, weight=None, reps=8, at=T + timedelta(seconds=60))
    assert tr.next_up(w) is squat
    for i in range(3):
        tr.log_set(db, squat, weight=None, reps=20, at=T + timedelta(minutes=2 + 2 * i))
        tr.log_set(db, pull, weight=None, reps=8, at=T + timedelta(minutes=3 + 2 * i))
    assert tr.next_up(w) is bench  # the superset is done – on to the next exercise
    tr.log_set(db, bench, weight=20, reps=12, at=T + timedelta(minutes=10))
    assert tr.next_up(w) is bench


def test_start_screen_and_away(auth_client, db, user):
    page = auth_client.get("/forma/trening/nowy").text
    assert "1. Gdzie trenujesz?" in page and "kolej na ten" in page and "Bez planu" in page
    plan = tr.plans(db)[0]
    r = _post(auth_client, "/forma/trening", {"where": "away", "plan_id": str(plan.id)})
    w = db.get(Workout, int(r.headers["location"].rsplit("/", 1)[1]))
    assert w.equipment == tr.away_equipment()
    assert all(we.exercise.equipment in tr.away_equipment() for we in w.exercises if we.swapped_from_id)


def test_exercise_pictures_exist():
    """Every mapped picture is shipped, every mapped key is in the catalog; most of the catalog has one."""
    from pathlib import Path

    from domownik.models import Exercise as Ex

    by_key, by_name = tr._images()
    keys = {e["key"] for e in tr.catalog()["exercises"]}
    assert set(by_key) <= keys
    static = Path(tr.__file__).parent / "static" / "exercises"
    for slug in {*by_key.values(), *by_name.values()}:
        assert (static / f"{slug}.webp").is_file(), slug
    assert len(by_key) >= 0.9 * len(keys)
    assert tr.image(Ex(key=None, name="Dumbbell Hammer Curl")) == "/static/exercises/hammer-curls.webp"
    assert tr.image(Ex(key=None, name="Coś własnego")) is None
    assert tr.equipment_icon(Ex(key=None, name="x", equipment="band")) == "🎗️"


# ---------------------------------------------------------------- ⓘ how an exercise is done


def test_every_catalog_exercise_has_a_guide():
    """A new exercise in exercises.yaml needs its instructions in exercise_guides.yaml too."""
    by_key, by_name = tr._guides()
    missing = [row["key"] for row in tr.catalog()["exercises"] if row["key"] not in by_key]
    assert not missing, f"brak instrukcji w exercise_guides.yaml: {missing}"
    images = tr.yaml.safe_load(tr.IMAGES.read_text(encoding="utf-8"))
    assert {n.lower() for n in images["by_name"]} <= set(by_name)
    for g in [*by_key.values(), *by_name.values()]:
        assert 3 <= len(g.steps) <= 5 and 1 <= len(g.mistakes) <= 2


def test_guide_by_key_and_by_jefit_name(db):
    tr.ensure_library(db)
    assert tr.guide(_ex(db, "back_squat")).steps
    jefit = Exercise(
        name="Dumbbell Hammer Curl", en="Dumbbell Hammer Curl", pattern="biceps", muscle="biceps", equipment="dumbbell"
    )
    assert tr.guide(jefit) is not None
    own = Exercise(name="Moje dziwne ćwiczenie", pattern="biceps", muscle="biceps", equipment="dumbbell")
    assert tr.guide(own) is None


def test_every_picture_has_its_end_frame(db):
    tr.ensure_library(db)
    for ex in db.query(Exercise).all():
        if tr.image(ex):
            assert tr.image_end(ex), ex.key


def test_guide_modal_and_page(auth_client, db):
    tr.ensure_library(db)
    db.commit()
    squat = _ex(db, "back_squat")
    modal = auth_client.get(f"/forma/cwiczenie/{squat.id}", headers={"HX-Request": "true", "HX-Target": "modal-body"})
    assert modal.status_code == 200
    assert "Jak wykonać" in modal.text and "Uważaj na" in modal.text and "-1.webp" in modal.text
    plain = auth_client.get(f"/forma/cwiczenie/{squat.id}", follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"] == f"/forma/postepy/{squat.id}"
    page = auth_client.get(f"/forma/postepy/{squat.id}").text
    assert "Jak wykonać" in page and "Jeszcze nie robione" in page
    assert auth_client.get("/forma/cwiczenie/999999", headers={"HX-Target": "modal-body"}).status_code == 404
