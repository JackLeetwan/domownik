from __future__ import annotations

from datetime import date, datetime

from domownik import jefit
from domownik import training as tr
from domownik.models import Exercise, Workout


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s + "+01:00").timestamp())


DATA = {
    "exercises": {
        "d_2": {"name": "Barbell Bench Press", "body": "Chest"},
        "d_86": {"name": "Leg Press", "body": "Upper Legs"},
        "d_999": {"name": "Cable Wood Chop", "body": "Abs"},
        "d_77": {"name": "Barbell Curl", "body": "Biceps"},
        "d_78": {"name": "Cable Kneeling Crunch (Rope)", "body": "Abs"},
    },
    "sessions": [
        # 9.11: three one-exercise sessions within the evening → one training
        {"id": 1, "t": _ts("2026-11-09T19:14:00"), "logs": [
            {"e": "d_2", "rt": "weight_and_reps", "ss": 10, "sets": [[176.37, 8, None, "default"]] * 4}]},
        {"id": 2, "t": _ts("2026-11-09T19:46:00"), "logs": [
            {"e": "d_86", "rt": "weight_and_reps", "ss": 11, "sets": [[130.07, 12, None, "default"], [135.14, 7, None, "failure"]]}]},
        {"id": 3, "t": _ts("2026-11-09T20:09:00"), "logs": [
            {"e": "d_999", "rt": "weight_and_reps", "ss": 12, "sets": [[22.05, 15, None, "default"]]}]},
        # October: a superset (two logs sharing "ss") and an empty log
        {"id": 4, "t": _ts("2026-10-13T18:00:00"), "logs": [
            {"e": "d_77", "rt": "weight_and_reps", "ss": 5, "sets": [[66.14, 12, None, "default"]] * 3},
            {"e": "d_78", "rt": "weight_and_reps", "ss": 5, "sets": [[88.18, 15, None, "default"]] * 3},
            {"e": "d_2", "rt": "weight_and_reps", "ss": 7, "sets": []}]},
    ],
}  # fmt: skip


def test_import(db, user):
    res = jefit.run(db, user, DATA)
    db.commit()
    assert (res.workouts, res.sessions, res.sets) == (2, 4, 13)
    assert res.new_exercises == ["Cable Wood Chop"]

    ws = tr.mine(db, user)
    nov = ws[0]
    assert nov.day == date(2026, 11, 9) and nov.source == "jefit"
    assert [we.exercise.key for we in nov.exercises] == ["bench_press", "leg_press", None]
    assert [(s.weight, s.reps) for s in nov.exercises[1].done] == [(59, 12), (61.25, 7)]
    assert tr.sets_text(list(nov.exercises[0].done)) == "80 kg × 8, 8, 8, 8"
    chop = db.query(Exercise).filter_by(name="Cable Wood Chop").one()
    assert (chop.equipment, chop.muscle, chop.step) == ("cable", "core", 2.5)

    octo = ws[1]
    assert [we.superset for we in octo.exercises] == [1, 1]  # the empty bench log is left out
    assert tr.week(db, user, date(2026, 11, 10)).done == 1  # Mon 9.11 = one training

    # progression starts from the imported weights
    plan = tr.seed_plans(db) or tr.plans(db)
    w = tr.start(db, user, plan[0], at=datetime(2026, 11, 12, 18, 0))
    curl = next(we for we in w.exercises if we.exercise.key == "bb_curl")
    assert tr.suggest(db, user, curl).last == "30 kg × 12, 12, 12"


def test_import_twice_adds_nothing(db, user):
    jefit.run(db, user, DATA)
    db.commit()
    res = jefit.run(db, user, DATA)
    assert res.workouts == 0
    assert db.query(Workout).count() == 2


def test_cli(tmp_path, db, user, monkeypatch):
    import json

    from typer.testing import CliRunner

    from domownik import cli
    from domownik.cli import app

    monkeypatch.setattr(cli, "migrate", lambda: None)  # the test database is made without alembic

    f = tmp_path / "jefit.json"
    f.write_text(json.dumps(DATA))
    out = CliRunner().invoke(app, ["import-jefit", str(f), "--email", user.email, "--dry-run"])
    assert out.exit_code == 0, out.output
    assert "(próba) Treningi: 2 (z 4 sesji Jefita), serie: 13" in out.output
    assert db.query(Workout).count() == 0
