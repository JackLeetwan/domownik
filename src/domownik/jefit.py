"""History from Jefit (its web API, saved as JSON) → private workouts of one person.

Input: {"exercises": {"d_000000000002": {"name": "Barbell Bench Press", "body": "Chest"}, …},
        "sessions": [{"id": 4180988861, "t": 1794248094, "logs": [{"e": "d_…", "rt": "weight_and_reps",
                      "ss": 1882, "lid": 1882, "sets": [[176.37, 8, null, "default"], …]}]}]}
Weights come in pounds (Jefit's storage) and become kg. Sessions of one day started within MERGE_GAP of
each other are one workout – Jefit users often finish a "workout" after a single exercise. An exercise
the library does not know (by its Jefit name) is added with a pattern guessed from its body part.
Running it again skips the sessions already imported (their Jefit ids are kept in the workout's note).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import training as tr
from domownik.config import get_settings
from domownik.models import Exercise, User, Workout, WorkoutExercise, WorkoutSet

LB = 0.45359237
MERGE_GAP = timedelta(hours=3)
# strength logs only: Jefit's duration / cardio logs are warm-ups and stretches without numbers
KINDS = ("weight_and_reps", "reps")
JEFIT_EQUIPMENT = {
    "barbell": ("barbell", 2.5),
    "ez_bar": ("barbell", 2.5),
    "dumbbell": ("dumbbell", 2.0),
    "cable": ("cable", 2.5),
    "machine": ("machine", 5.0),
    "smith_machine": ("machine", 2.5),
    "band": ("band", 0.0),
    "kettlebell": ("kettlebell", 4.0),
    "pull_up_bar": ("bar", 0.0),
}
NOTE_PREFIX = "jefit:"

# Jefit body part → (muscle, pattern) of an exercise the library does not have
BODY = {
    "chest": ("chest", "push_h"),
    "back": ("back", "pull_h"),
    "middle back": ("back", "pull_h"),
    "lower back": ("posterior", "hinge"),
    "traps": ("shoulders", "rear_delt"),
    "inner chest": ("chest", "fly"),
    "outer chest": ("chest", "push_h"),
    "obliques": ("core", "core_flex"),
    "lats": ("back", "pull_v"),
    "shoulders": ("shoulders", "lateral"),
    "biceps": ("biceps", "biceps"),
    "triceps": ("triceps", "triceps"),
    "forearms": ("biceps", "biceps"),
    "abs": ("core", "core_flex"),
    "upper legs": ("quads", "squat"),
    "quads": ("quads", "squat"),
    "hamstrings": ("posterior", "ham_iso"),
    "glutes": ("posterior", "glute"),
    "lower legs": ("calves", "calves"),
    "calves": ("calves", "calves"),
}
EQUIPMENT = [
    (r"\bbarbell\b|\bez[- ]bar\b", "barbell", 2.5),
    (r"\bdumbbell\b", "dumbbell", 2.0),
    (r"\bcable\b|\bpulley\b", "cable", 2.5),
    (r"\bmachine\b|\blever\b|\bsmith\b|\bsled\b|\bleg press\b", "machine", 5.0),
    (r"\bband\b", "band", 0.0),
    (r"\bkettlebell\b", "kettlebell", 4.0),
]


def kg(lb: float | None) -> float | None:
    if not lb:
        return None
    return round(lb * LB * 4) / 4 if lb * LB >= 20 else round(lb * LB, 1)  # 59 kg, 61.25 kg; 11.3 kg


@dataclass
class Result:
    workouts: int = 0
    sessions: int = 0
    sets: int = 0
    skipped: int = 0
    new_exercises: list[str] = field(default_factory=list)


def _library(db: Session) -> dict[str, Exercise]:
    out: dict[str, Exercise] = {}
    for ex in db.scalars(select(Exercise)):
        for alias in [*(ex.en or "").split("|"), ex.name]:
            if alias:
                out.setdefault(alias.strip().lower(), ex)
    return out


def _exercise(db: Session, lib: dict[str, Exercise], info: dict[str, Any], res: Result) -> Exercise:
    name = (info.get("name") or "?").strip()
    ex = lib.get(name.lower())
    if ex is not None:
        return ex
    muscle, pattern = BODY.get((info.get("body") or "").strip().lower().replace("_", " "), ("core", "core_static"))
    equipment, step = "bodyweight", 0.0
    for rx, eq, st in EQUIPMENT:
        if re.search(rx, name, re.I):
            equipment, step = eq, st
            break
    else:
        for eq in info.get("eq") or []:
            if eq in JEFIT_EQUIPMENT:
                equipment, step = JEFIT_EQUIPMENT[eq]
                break
    timed = False
    ex = Exercise(
        name=name[:80],
        en=name[:400],
        pattern=pattern,
        muscle=muscle,
        equipment=equipment,
        kind="time" if timed else "reps",
        step=step,
    )
    db.add(ex)
    db.flush()
    lib[name.lower()] = ex
    res.new_exercises.append(name)
    return ex


def _local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, get_settings().tz).replace(tzinfo=None)


def _imported_ids(db: Session, user: User) -> set[str]:
    ids: set[str] = set()
    for note in db.scalars(select(Workout.note).where(Workout.user_id == user.id, Workout.source == "jefit")):
        if note and note.startswith(NOTE_PREFIX):
            ids.update(note[len(NOTE_PREFIX) :].split(","))
    return ids


def groups(sessions: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Sessions in time order, those of one day started within MERGE_GAP of the previous one together."""
    out: list[list[dict[str, Any]]] = []
    for s in sorted(sessions, key=lambda s: s["t"]):
        if out:
            prev = out[-1][-1]
            a, b = _local(prev["t"]), _local(s["t"])
            if a.date() == b.date() and b - a <= MERGE_GAP:
                out[-1].append(s)
                continue
        out.append([s])
    return out


def run(db: Session, user: User, data: dict[str, Any]) -> Result:
    tr.ensure_library(db)
    lib = _library(db)
    infos = data.get("exercises") or {}
    res = Result()
    done = _imported_ids(db, user)
    for group in groups(data.get("sessions") or []):
        group = [s for s in group if str(s["id"]) not in done]
        if not group or not any(
            log.get("rt") in KINDS and any(x[0] or x[1] for x in log.get("sets") or [])
            for s in group
            for log in s.get("logs") or []
        ):
            res.skipped += 1 if group else 0
            continue
        start = _local(group[0]["t"])
        w = Workout(
            user_id=user.id,
            day=start.date(),
            started_at=start,
            finished_at=start,
            source="jefit",
            note=(NOTE_PREFIX + ",".join(str(s["id"]) for s in group))[:300],
        )
        db.add(w)
        db.flush()
        pos, ss_numbers = 0, {}
        for s in group:
            t0 = _local(s["t"])
            logs = s.get("logs") or []
            shared = {log.get("ss") for log in logs if sum(1 for x in logs if x.get("ss") == log.get("ss")) > 1}
            for log in logs:
                log = {**log, "sets": [x for x in log.get("sets") or [] if x[0] or x[1]]}
                if not log["sets"] or log.get("rt") not in KINDS:
                    continue
                ex = _exercise(db, lib, infos.get(log["e"]) or {"name": log["e"]}, res)
                key = (s["id"], log.get("ss"))
                superset = None
                if log.get("ss") in shared:
                    superset = ss_numbers.setdefault(key, len(ss_numbers) + 1)
                sets = log["sets"]
                timed = False
                reps = [r for _, r, _, _ in sets if r]
                we = WorkoutExercise(
                    exercise_id=ex.id,
                    position=pos,
                    sets=len(sets),
                    reps_min=min(reps) if reps and not timed else None,
                    reps_max=max(reps) if reps and not timed else None,
                    seconds=None,
                    rest_seconds=tr.DEFAULT_REST,
                    superset=superset,
                )
                w.exercises.append(we)
                pos += 1
                for i, (lb, r, interval, _kind) in enumerate(sets, 1):
                    we.done.append(
                        WorkoutSet(
                            set_no=i,
                            weight=kg(lb),
                            reps=None if timed else (r or None),
                            seconds=(interval or r) if timed else None,
                            done_at=t0 + timedelta(minutes=2 * i),
                        )
                    )
                    res.sets += 1
                w.finished_at = max(w.finished_at, t0 + timedelta(minutes=2 * len(sets)))
            res.sessions += 1
        res.workouts += 1
    db.flush()
    return res
