from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from domownik import training as tr
from domownik import webpush
from domownik.models import Exercise, PushSubscription, User, Workout, WorkoutPlan
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


def test_loaded_hold_logs_weight(db, user, fbw, auth_client):
    """Farmer's walk: time with a load – the weight is asked for, kept in the history and suggested again."""
    w1 = tr.start(db, user, fbw[0], at=T - timedelta(days=3))
    fw = tr.add_exercise(db, w1, _ex(db, "farmers_walk"))
    for secs in (40, 35):
        tr.log_set(db, fw, weight=24, reps=None, seconds=secs, at=T - timedelta(days=3))
    tr.finish(db, w1, T - timedelta(days=3))
    assert tr.sets_text(list(fw.done)) == "24 kg × 40 s, 35 s"
    w2 = tr.start(db, user, fbw[0], at=T)
    fw = tr.add_exercise(db, w2, _ex(db, "farmers_walk"))
    s = tr.suggest(db, user, fw)
    assert (s.weight, s.seconds) == (24, 40)
    plank = tr.add_exercise(db, w2, _ex(db, "plank"))
    assert tr.suggest(db, user, plank).weight is None
    db.commit()

    screen = auth_client.get(f"/forma/trening/{w2.id}").text
    form = screen.split(f'/cw/{fw.id}/seria"', 1)[1].split("</form>", 1)[0]
    assert 'name="weight"' in form and 'name="seconds"' in form
    plank_form = screen.split(f'/cw/{plank.id}/seria"', 1)[1].split("</form>", 1)[0]
    assert 'name="weight"' not in plank_form
    r = auth_client.post(
        f"/forma/trening/{w2.id}/cw/{fw.id}/seria",
        data={"weight": "26", "seconds": "45"},
        headers={"HX-Request": "true", "HX-Target": "workout"},
    )
    assert r.status_code == 200 and "26 kg × 45 s" in r.text


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
    assert w.rest_until == T + timedelta(seconds=120)
    # ±s: the current rest moves and the exercise keeps the new length (the plan does not change)
    w.rest_pushed = True
    tr.adjust_rest(db, w, 30, at=T + timedelta(seconds=10))
    assert w.rest_until == T + timedelta(seconds=150) and pull.rest_seconds == 150 and not w.rest_pushed
    tr.adjust_rest(db, w, -30, at=T + timedelta(seconds=10))
    tr.adjust_rest(db, w, -30, at=T + timedelta(seconds=10))
    assert w.rest_until == T + timedelta(seconds=90) and pull.rest_seconds == 90
    tr.adjust_rest(db, w, -60, at=T + timedelta(seconds=50))  # never into the past: the rest is over now
    assert w.rest_until == T + timedelta(seconds=50)
    tr.adjust_rest(db, w, 30, at=T + timedelta(seconds=70))  # ran out → +30 s from now
    assert w.rest_until == T + timedelta(seconds=100) and pull.rest_seconds == 60
    assert all(it.rest_seconds == 120 for it in fbw[0].items if it.superset == 1)
    backfill = tr.start(db, user, fbw[1], day=D - timedelta(days=1), at=T)
    tr.log_set(db, backfill.exercises[0], weight=20, reps=12, at=T)
    assert backfill.rest_until is None  # an old day typed in later has no rest


def test_rest_push(db, user, fbw, sent):
    db.add(PushSubscription(user_id=user.id, endpoint="https://push.example.com/x", p256dh="pk", auth="au"))
    w = tr.start(db, user, fbw[0], at=T)
    tr.log_set(db, w.exercises[2], weight=20, reps=12, at=T)
    db.commit()
    assert tr.run_rest(db, T + timedelta(seconds=119)) == 0  # dumbbell bench: 2 min
    assert tr.run_rest(db, T + timedelta(seconds=120)) == 1
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
    assert (
        "Przysiad hiszpański z gumą (spanish squat)" in screen
        and "Superseria A" in screen
        and 'class="wo-swap"' in screen
    )
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
    assert f'hx-post="/forma/trening/{wid}/przerwa?o=15"' in r.text and "−15 s" in r.text
    from domownik.web import ASSET_V

    assert r.headers["X-Asset-V"] == ASSET_V  # a page from before a deploy reloads after a partial swap too
    rest = bench.rest_seconds
    hx = {"HX-Request": "true", "HX-Target": "workout"}
    r = auth_client.post(f"/forma/trening/{wid}/przerwa?o=15", headers=hx)
    assert r.status_code == 200 and f"przerwa {rest + 15} s" in r.text
    auth_client.post(f"/forma/trening/{wid}/przerwa?o=30", headers=hx)  # only ±15
    db.refresh(bench)
    assert bench.rest_seconds == rest + 15
    # the plan's sets done: "＋ jeszcze jedna seria" opens the usual form – its own weight and reps
    for _ in range(bench.sets - len(bench.done)):
        tr.log_set(db, bench, weight=25, reps=8)
    db.commit()
    screen = auth_client.get(f"/forma/trening/{wid}").text
    more = screen.split(f'id="we{bench.id}"', 1)[1].split("</article>", 1)[0]
    assert '<details class="wo-more"' in more and "＋ jeszcze jedna seria" in more
    assert 'name="weight" inputmode="decimal" value="25"' in more and f"✓ Seria {bench.sets + 1}<" in more
    assert "&lt;form" not in more  # the folded form is markup, not text
    for s in bench.done[-(bench.sets - 1) :]:
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


def test_abandoned_empty_start_is_dropped(auth_client, db, user, fbw):
    old = tr.start(db, user, fbw[0], at=datetime.now() - timedelta(days=1))
    kept = tr.start(db, user, fbw[1], at=datetime.now() - timedelta(days=2))
    tr.log_set(db, kept.exercises[0], weight=20, reps=10)
    db.commit()
    old_id, kept_id = old.id, kept.id
    auth_client.get("/forma")
    db.expire_all()
    assert db.get(Workout, old_id) is None and db.get(Workout, kept_id) is not None


def test_add_picker_shows_recent_first(auth_client, db, user, fbw):
    done = tr.start(db, user, None, at=datetime.now() - timedelta(days=1))
    tr.add_exercise(db, done, _ex(db, "leg_extension"))
    tr.log_set(db, done.exercises[0], weight=36, reps=12)
    tr.finish(db, done)
    w = tr.start(db, user, None)
    db.commit()
    page = auth_client.get(f"/forma/trening/{w.id}/dodaj").text
    recent = page.split("🕘 Ostatnio robione", 1)[1].split("</section>", 1)[0]
    assert "Prostowanie nóg na maszynie" in recent
    assert "Ostatnio robione" not in auth_client.get(f"/forma/trening/{w.id}/dodaj?partia=chest").text
    assert "Nic nie pasuje" not in page.split("<title>", 1)[1].split("</title>", 1)[0]


def test_save_made_up_workout_as_plan(auth_client, db, user, fbw):
    w = tr.start(db, user, None)
    a, b, c = (tr.add_exercise(db, w, _ex(db, k)) for k in ("leg_press", "leg_extension", "leg_curl"))
    tr.link_superset(db, w, b, c)
    for _ in range(4):
        tr.log_set(db, a, weight=100, reps=10)
    for we in (b, c):
        tr.log_set(db, we, weight=30, reps=12)
    db.commit()
    page = auth_client.get(f"/forma/trening/{w.id}").text
    assert f'action="/forma/trening/{w.id}/plan"' in page
    r = auth_client.post(f"/forma/trening/{w.id}/plan", data={"name": "Nogi rano"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    p = db.query(WorkoutPlan).filter_by(name="Nogi rano").one()
    assert [(i.exercise.key, i.sets, i.superset) for i in p.items] == [
        ("leg_press", 4, None),
        ("leg_extension", 3, 1),
        ("leg_curl", 3, 1),
    ]
    assert db.get(Workout, w.id).plan_id == p.id
    assert tr.next_plan(db, user).id != p.id  # not in the FBW cycle
    assert "Zapisz jako plan" not in auth_client.get(f"/forma/trening/{w.id}").text


def test_log_a_whole_superset_round(auth_client, db, user, fbw):
    w = tr.start(db, user, None)
    b, c = (tr.add_exercise(db, w, _ex(db, k)) for k in ("leg_extension", "leg_curl"))
    tr.link_superset(db, w, b, c)
    db.commit()
    hx = {"HX-Request": "true", "HX-Target": "workout"}
    page = auth_client.get(f"/forma/trening/{w.id}").text
    assert f'hx-post="/forma/trening/{w.id}/runda/{b.superset}"' in page and "Zapisz rundę 1/3" in page
    r = auth_client.post(
        f"/forma/trening/{w.id}/runda/{b.superset}",
        data={f"weight_{b.id}": "36", f"reps_{b.id}": "12", f"weight_{c.id}": "50", f"reps_{c.id}": "12"},
        headers=hx,
    )
    db.expire_all()
    w = db.get(Workout, w.id)
    assert [(s.weight, s.reps) for we in w.exercises for s in we.done] == [(36, 12), (50, 12)]
    assert w.rest_until is not None  # the rest after the round
    assert "Zapisz rundę 2/3" in r.text
    r = auth_client.post(f"/forma/trening/{w.id}/runda/{b.superset}", data={f"weight_{b.id}": "36"}, headers=hx)
    assert "wpisz liczbę powtórzeń" in r.text
    # one logged alone: mid-round, the usual forms
    tr.log_set(db, w.exercises[0], weight=36, reps=12)
    db.commit()
    assert "Zapisz rundę" not in auth_client.get(f"/forma/trening/{w.id}").text


def test_imported_english_exercises_are_adopted(db, user):
    """Made by the Jefit import under English names: they become the catalog ones with Polish names."""
    tr.ensure_library(db)
    side = Exercise(
        name="Dumbbell Side Bend",
        en="Dumbbell Side Bend",
        pattern="lateral",
        muscle="shoulders",
        equipment="bodyweight",
    )
    dup = Exercise(name="Scissor Kick", en="Scissor Kick", pattern="core_flex", muscle="core", equipment="bodyweight")
    own = Exercise(name="Moje ćwiczenie", pattern="biceps", muscle="biceps", equipment="dumbbell")
    db.add_all([side, dup, own])
    db.flush()
    db.delete(_ex(db, "db_side_bend"))  # as on the phone before: no catalog row yet → the imported one takes the key
    db.flush()
    w = tr.start(db, user, None)
    we = tr.add_exercise(db, w, dup)  # the catalog row exists → the history moves to it
    tr.log_set(db, we, weight=None, reps=20)
    db.commit()
    side_id = side.id
    tr.ensure_library(db)
    db.commit()
    db.expire_all()
    s = db.get(Exercise, side_id)
    assert (s.key, s.name, s.pattern, s.muscle, s.equipment) == (
        "db_side_bend",
        "Skłony boczne z hantlem",
        "core_flex",
        "core",
        "dumbbell",
    )
    kick = _ex(db, "scissor_kick")
    assert db.get(Workout, w.id).exercises[0].exercise_id == kick.id and kick.name == "Nożyce"
    assert db.query(Exercise).filter_by(name="Scissor Kick").count() == 0
    assert db.query(Exercise).filter_by(name="Moje ćwiczenie").one().key is None
    assert tr.guide(s) is not None and tr.image(s)


def test_catalog_names_are_polish():
    """No bare English names: a gym term may stay only in brackets after the Polish name."""
    import re

    english = re.compile(
        r"\b(press|curl|row|raise|squat|pull|push|fly|kick|twist|plank|bench|cable|dumbbell|barbell)\b", re.I
    )
    bad = [r["name"] for r in tr.catalog()["exercises"] if english.search(re.sub(r"\([^)]*\)", "", r["name"]))]
    assert not bad, bad


def test_added_on_the_spot_takes_the_last_range(db, user, fbw):
    """An exercise added without a plan: sets, rep range and rest as last time – the progress is judged there."""
    first = tr.start(db, user, None, at=T - timedelta(days=7))
    new = tr.add_exercise(db, first, _ex(db, "leg_extension"))
    assert (new.sets, new.reps_min, new.reps_max) == (3, 8, 12)  # never done: the usual
    plan_w = tr.start(db, user, fbw[0], at=T - timedelta(days=3))
    sq = next(we for we in plan_w.exercises if we.exercise.key == "spanish_squat")
    sq.rest_seconds = 75
    for _ in range(5):
        tr.log_set(db, sq, weight=None, reps=20, at=T - timedelta(days=3))
    db.commit()
    w = tr.start(db, user, None, at=T)
    we = tr.add_exercise(db, w, sq.exercise)
    assert (we.sets, we.reps_min, we.reps_max, we.rest_seconds) == (5, sq.reps_min, 20, 75)
    sug = tr.suggest(db, user, we)
    assert sug.reps == 21 and sug.note == "więcej powtórzeń"  # not "+kg, 8 reps"

    # from the Jefit history: the range is a usual one from the top done
    old = Workout(user_id=user.id, day=D - timedelta(days=30), started_at=T - timedelta(days=30), source="jefit")
    db.add(old)
    db.flush()
    jw = tr.add_exercise(db, old, _ex(db, "lat_pulldown"))
    jw.reps_min = jw.reps_max = 15
    tr.log_set(db, jw, weight=40, reps=15, at=T - timedelta(days=30))
    db.commit()
    got = tr.add_exercise(db, w, _ex(db, "lat_pulldown"))
    assert (got.reps_min, got.reps_max) == tr.range_for(15) and got.rest_seconds == tr.suggested_rest(
        _ex(db, "lat_pulldown")
    )


def test_records_summary_and_folding(db, user, fbw, auth_client):
    """🏆 a set better than everything before; the card on top of a finished workout; done exercises fold."""
    old = T - timedelta(days=7)
    w1 = tr.start(db, user, fbw[0], at=old)
    _do(db, w1, {"db_bench": [(30, 10), (30, 9)]}, at=old)
    tr.finish(db, w1, old + timedelta(minutes=50))
    w2 = tr.start(db, user, fbw[0], at=T)
    _do(db, w2, {"db_bench": [(32, 10), (30, 8), (32, 9), (32, 8)]})
    bench = next(we for we in w2.exercises if we.exercise.key == "db_bench")
    recs = tr.records(w2, tr.prior_best(db, w2))
    assert set(recs) == {bench.id} and (recs[bench.id].weight, recs[bench.id].reps) == (32, 10)
    db.commit()

    live = auth_client.get(f"/forma/trening/{w2.id}").text
    assert "🏆 rekord" in live and 'class="wo-ex complete folded"' in live and "⋯ Więcej" in live
    assert "Podsumowanie treningu" not in live

    tr.finish(db, w2, T + timedelta(minutes=55))
    db.commit()
    done = auth_client.get(f"/forma/trening/{w2.id}").text
    assert "Podsumowanie treningu" in done and "<b>55</b> min" in done and "<b>4</b> serie" in done
    assert "🏆 Rekordy" in done and "Wyciskanie hantli na ławce płaskiej <b>32 kg × 10</b>" in done
    s = tr.summary(db, w2, recs)
    assert s.volume == 32 * 10 + 30 * 8 + 32 * 9 + 32 * 8 and s.prev_volume == 30 * 19 and s.change > 0
    # the first time ever is no record – nothing to beat
    assert tr.records(w1, tr.prior_best(db, w1)) == {}


def test_progress_list_and_history_ranges(db, user, fbw, auth_client):
    t = date.today()
    for days, kg in ((400, 20), (100, 24), (10, 26)):
        at = datetime.combine(t - timedelta(days=days), datetime.min.time()).replace(hour=18)
        w = tr.start(db, user, fbw[0], at=at)
        _do(db, w, {"db_bench": [(kg, 10)]}, at=at)
        tr.finish(db, w, at + timedelta(minutes=40))
    db.commit()
    bench = _ex(db, "db_bench")
    page = auth_client.get("/forma/postepy").text
    assert "Ostatnie 30 dni" in page and "data-pick-filter" in page and "🏆 ~34,7 kg" in page

    year = auth_client.get(f"/forma/postepy/{bench.id}").text  # the last year by default
    assert 'aria-current="page">Rok' in year and "pokaż całość" in year
    assert "~32&nbsp;kg" in year and "~26,7&nbsp;kg" not in year  # 400 days ago: outside the year
    assert "🏆</b>" in year
    every = auth_client.get(f"/forma/postepy/{bench.id}?okres=all").text
    assert "pokaż całość" not in every and every.count('class="muted tr-best"') == 3
    assert "🏆" in every


def test_set_marks_warmup_failure_drop(auth_client, db, user, fbw):
    """🌡 a warm-up set is shown but not counted; 🔥 to failure stops "add weight"; ↘ drops stay in one set."""
    w1 = tr.start(db, user, fbw[0], at=T - timedelta(days=7))
    bench = next(we for we in w1.exercises if we.exercise.key == "db_bench")
    tr.log_set(db, bench, weight=10, reps=15, warmup=True, at=T - timedelta(days=7))
    for i in range(4):
        tr.log_set(db, bench, weight=20, reps=12, failure=i == 3, at=T - timedelta(days=7))
    assert len(bench.done) == 5 and len(bench.work) == 4
    tr.finish(db, w1, T - timedelta(days=7))

    w2 = tr.start(db, user, fbw[0], at=T)
    bench = next(we for we in w2.exercises if we.exercise.key == "db_bench")
    s = tr.suggest(db, user, bench)
    assert s.last == "20 kg × 12, 12, 12, 12🔥"  # no warm-up in "last time"
    assert (s.weight, s.up) == (20, False)  # the 12 came only to failure – not +2 kg yet
    db.commit()

    hx = {"HX-Request": "true", "HX-Target": "workout"}
    url = f"/forma/trening/{w2.id}/cw/{bench.id}/seria"
    page = auth_client.get(f"/forma/trening/{w2.id}").text
    assert 'name="failure"' in page and 'name="drop_w"' in page and 'data-reps-max="12"' in page
    assert f'/cw/{bench.id}/zamien" class="wo-swap"' in page  # ⇄ by the name before the first set
    auth_client.post(url, data={"weight": "12", "reps": "15", "warmup": "1"}, headers=hx)
    r = auth_client.post(
        url,
        data={"weight": "20", "reps": "10", "drop": "1", "drop_w": ["16", "", "12"], "drop_r": ["6", "", "4"]},
        headers=hx,
    )
    db.expire_all()
    we = next(x for x in db.get(Workout, w2.id).exercises if x.id == bench.id)
    warm, work = we.done
    assert warm.warmup and not work.warmup and we.work == [work]
    assert work.drops == [{"w": 16.0, "r": 6}, {"w": 12.0, "r": 4}]
    assert tr.set_text(work) == "20 kg × 10 ↘ 16 kg × 6 ↘ 12 kg × 4"
    assert "Seria 2/4" in r.text and "rozgrzewka" in r.text
    assert f'/cw/{bench.id}/zamien" class="wo-swap"' not in r.text
    # drops without the ↘ toggle are ignored; a drop with letters is an error
    auth_client.post(url, data={"weight": "20", "reps": "10", "drop_w": "16", "drop_r": "6"}, headers=hx)
    db.expire_all()
    assert next(x for x in db.get(Workout, w2.id).exercises if x.id == bench.id).done[-1].drops is None
    r = auth_client.post(url, data={"reps": "10", "drop": "1", "drop_r": "x"}, headers=hx)
    assert "Drop set" in r.text


def test_warmup_not_a_record(db, user, fbw):
    w1 = tr.start(db, user, fbw[0], at=T - timedelta(days=7))
    _do(db, w1, {"db_bench": [(20, 10)]}, at=T - timedelta(days=7))
    tr.finish(db, w1, T - timedelta(days=7))
    w2 = tr.start(db, user, fbw[0], at=T)
    bench = next(we for we in w2.exercises if we.exercise.key == "db_bench")
    tr.log_set(db, bench, weight=30, reps=10, warmup=True, at=T)
    tr.log_set(db, bench, weight=20, reps=8, at=T)
    assert tr.records(w2, tr.prior_best(db, w2)) == {}
    assert tr.summary(db, w2, {}).sets == 1


def test_workout_bubble_on_other_pages(auth_client, db, user):
    w = tr.start(db, user, None)
    db.commit()
    assert 'class="wo-bubble"' in auth_client.get("/notatki").text
    assert 'class="wo-bubble"' not in auth_client.get(f"/forma/trening/{w.id}").text
    tr.finish(db, w)
    db.commit()
    assert 'class="wo-bubble"' not in auth_client.get("/notatki").text


def test_exercise_rest_in_place(db, user, fbw):
    w = tr.start(db, user, fbw[0], at=T)
    squat, pull, bench = w.exercises[0], w.exercises[1], w.exercises[2]
    assert not tr.rest_holder(squat) and tr.rest_holder(pull) and tr.rest_holder(bench)
    tr.set_exercise_rest(db, bench, 15)
    tr.set_exercise_rest(db, bench, 15)
    assert bench.rest_seconds == 150 and tr.plan_item_for(bench).rest_seconds == 120  # this workout only
    tr.log_set(db, bench, weight=20, reps=10, at=T)
    assert w.rest_until == T + timedelta(seconds=150)
    tr.set_exercise_rest(db, bench, 0, to_plan=True)  # 📌 into the plan
    assert tr.plan_item_for(bench).rest_seconds == 150
    tr.set_exercise_rest(db, bench, -15 * 100)
    assert bench.rest_seconds == 0


def test_exercise_rest_on_the_screen(auth_client, db, user):
    auth_client.get("/forma")  # seeds the plans
    plan = tr.plans(db)[0]
    r = _post(auth_client, "/forma/trening", {"plan_id": str(plan.id)})
    wid = int(r.headers["location"].rsplit("/", 1)[1])
    w = db.get(Workout, wid)
    bench = next(we for we in w.exercises if we.exercise.key == "db_bench")
    squat = w.exercises[0]  # first of a superset – its rest is not the one after it
    screen = auth_client.get(f"/forma/trening/{wid}").text
    art = screen.split(f'id="we{bench.id}"', 1)[1].split("</article>", 1)[0]
    assert '<details class="wo-rest-edit">' in art and "⏱ przerwa 120 s" in art and "📌" not in art
    first = screen.split(f'id="we{squat.id}"', 1)[1].split("</article>", 1)[0]
    assert "wo-rest-edit" not in first
    hx = {"HX-Request": "true", "HX-Target": "workout"}
    url = f"/forma/trening/{wid}/cw/{bench.id}/przerwa"
    r = auth_client.post(url + "?o=15", headers=hx)
    art = r.text.split(f'id="we{bench.id}"', 1)[1].split("</article>", 1)[0]
    assert '<details class="wo-rest-edit" open>' in art and "⏱ przerwa 135 s" in art and "(w planie 120 s)" in art
    assert "📌 135 s na stałe w planie" in art
    auth_client.post(url + "?o=40", headers=hx)  # only ±15
    auth_client.post(url + "?plan=1", headers=hx)
    db.refresh(bench)
    item = tr.plan_item_for(bench)
    db.refresh(item)
    assert bench.rest_seconds == 135 and item.rest_seconds == 135


def test_suggested_rest(db, fbw):
    rest = {it.exercise.key: it.rest_seconds for it in fbw[0].items + fbw[1].items + fbw[2].items}
    assert rest["db_bench"] == 120 and rest["bb_row"] == 150 and rest["db_sldl"] == 120
    assert rest["lateral_raise"] == 60 and rest["skull_crusher"] == 90
    assert rest["spanish_squat"] == rest["pull_up"] == 120  # superset: the longest of the round
    assert rest["plank"] == rest["db_calf"] == 60
    assert rest["hip_thrust"] == rest["db_close_press"] == 150  # barbell hip thrust
    duo = {it.exercise.key: it.rest_seconds for it in fbw[3].items}
    assert duo["db_curl"] == duo["rope_pushdown"] == 60  # set in the catalog by hand
    assert tr.suggested_rest(_ex(db, "back_squat")) == 180 and tr.suggested_rest(_ex(db, "push_up")) == 90
    assert tr.suggested_rest(_ex(db, "hip_abduction")) == 60
