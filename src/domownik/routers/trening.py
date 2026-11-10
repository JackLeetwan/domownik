"""🏋️ Training in "Forma": start, the workout screen on the phone, substitutes, plans, progress.

Workouts are private (another person's id is a 404); the exercise library and the plans are shared.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from domownik import training as tr
from domownik.config import get_settings, today
from domownik.db import get_db
from domownik.models import Exercise, PlanItem, User, Workout, WorkoutExercise, WorkoutPlan, WorkoutSet
from domownik.security import current_user, verify_csrf
from domownik.web import is_htmx, render

router = APIRouter(prefix="/forma")


def _go(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _workout(db: Session, user: User, workout_id: int) -> Workout:
    w = tr.get_own(db, user, workout_id)
    if w is None:
        raise HTTPException(404, "Nie ma takiego treningu")
    return w


def _we(w: Workout, we_id: int) -> WorkoutExercise:
    we = next((e for e in w.exercises if e.id == we_id), None)
    if we is None:
        raise HTTPException(404, "Nie ma takiego ćwiczenia w treningu")
    return we


def _exercise(db: Session, exercise_id: int) -> Exercise:
    ex = db.get(Exercise, exercise_id)
    if ex is None:
        raise HTTPException(404, "Nie ma takiego ćwiczenia")
    return ex


def _plan(db: Session, plan_id: int) -> WorkoutPlan:
    p = db.get(WorkoutPlan, plan_id)
    if p is None:
        raise HTTPException(404, "Nie ma takiego planu")
    return p


def section_context(db: Session, user: User) -> dict:
    """The training part of /forma."""
    tr.ensure_library(db)
    tr.seed_plans(db)
    db.commit()
    t = today()
    wk = tr.week(db, user, t)
    monday = t - timedelta(days=t.weekday())
    letters = ["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"]
    return {
        "week": wk,
        "week_dots": [
            (letters[i], monday + timedelta(days=i), monday + timedelta(days=i) in wk.days) for i in range(7)
        ],
        "running": tr.active(db, user),
        "next_plan": tr.next_plan(db, user),
        "plans": tr.plans(db),
        "recent": [w for w in tr.mine(db, user, limit=8) if w.finished_at is not None][:5],
        "today": t,
    }


# ---------------------------------------------------------------- start


def _parse_day(raw) -> date | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    d = date.fromisoformat(raw)
    if d > today():
        raise ValueError("Trening z przyszłości? Wybierz dzisiejszy albo wcześniejszy dzień")
    return d


@router.post("/trening", dependencies=[Depends(verify_csrf)])
async def start(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    raw_plan = str(form.get("plan_id") or "")
    plan = _plan(db, int(raw_plan)) if raw_plan.isdigit() else None
    day = _parse_day(form.get("day"))
    running = tr.active(db, user)
    if running is not None and (day is None or day == today()) and running.day == today():
        return _go(f"/forma/trening/{running.id}")  # one going today already – back to it
    w = tr.start(db, user, plan, day=day, away=bool(form.get("away")))
    db.commit()
    return _go(f"/forma/trening/{w.id}")


# ---------------------------------------------------------------- the workout screen


def _screen(db: Session, user: User, w: Workout, errors: dict | None = None) -> dict:
    tz = get_settings().tz
    nxt = tr.next_up(w)
    rows = []
    for we in w.exercises:
        sug = tr.suggest(db, user, we)
        last_done = we.done[-1] if we.done else None
        rows.append(
            {
                "we": we,
                "sug": sug,
                "weight": last_done.weight if last_done and last_done.weight is not None else sug.weight,
                "reps": sug.reps if not last_done else (last_done.reps or sug.reps),
                "seconds": last_done.seconds if last_done and last_done.seconds else sug.seconds,
                "complete": len(we.done) >= we.sets,
                "current": nxt is not None and nxt.id == we.id,
                "sets_text": tr.sets_text(list(we.done)),
            }
        )
    rest_ms = None
    if w.rest_until is not None and w.finished_at is None:
        rest_ms = int(w.rest_until.replace(tzinfo=tz).timestamp() * 1000)
    total = sum(we.sets for we in w.exercises)
    done = sum(min(len(we.done), we.sets) for we in w.exercises)
    return {
        "user": user,
        "w": w,
        "rows": rows,
        "rest_ms": rest_ms,
        "next_we": nxt,
        "progress": (done, total),
        "errors": errors or {},
        "fmt_kg": tr.fmt_kg,
        "superset_letter": lambda n: "ABCDEFGH"[(n - 1) % 8] if n else "",
    }


def _show(request: Request, db: Session, user: User, w: Workout, errors: dict | None = None):
    ctx = _screen(db, user, w, errors)
    if is_htmx(request) and request.headers.get("hx-target") == "workout":
        return render(request, "partials/workout_body.html", ctx)
    return render(request, "workout.html", ctx)


@router.get("/trening/{workout_id}")
def workout_page(workout_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "workout.html", _screen(db, user, _workout(db, user, workout_id)))


def _num(raw, *, integer: bool = False, label: str) -> float | int | None:
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{label}: wpisz liczbę") from None
    if not 0 <= value <= 1000:
        raise ValueError(f"{label}: od 0 do 1000")
    return round(value) if integer else value


@router.post("/trening/{workout_id}/cw/{we_id}/seria", dependencies=[Depends(verify_csrf)])
async def log_set(
    workout_id: int, we_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    w = _workout(db, user, workout_id)
    we = _we(w, we_id)
    form = await request.form()
    try:
        weight = _num(form.get("weight"), label="Ciężar")
        if we.exercise.kind == "time":
            seconds = _num(form.get("seconds"), integer=True, label="Czas")
            if not seconds:
                raise ValueError("Wpisz czas w sekundach")
            tr.log_set(db, we, weight=weight, reps=None, seconds=seconds)
        else:
            reps = _num(form.get("reps"), integer=True, label="Powtórzenia")
            if not reps:
                raise ValueError("Wpisz liczbę powtórzeń")
            tr.log_set(db, we, weight=weight, reps=reps)
    except ValueError as exc:
        return _show(request, db, user, w, {we.id: str(exc)})
    db.commit()
    return _show(request, db, user, w)


@router.post("/trening/{workout_id}/seria/{set_id}/usun", dependencies=[Depends(verify_csrf)])
def undo_set(
    workout_id: int, set_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    w = _workout(db, user, workout_id)
    s = db.get(WorkoutSet, set_id)
    if s is None or s.workout_exercise.workout_id != w.id:
        raise HTTPException(404, "Nie ma takiej serii")
    tr.undo_set(db, s)
    db.commit()
    return _show(request, db, user, w)


@router.post("/trening/{workout_id}/przerwa-koniec", dependencies=[Depends(verify_csrf)])
def skip_rest(workout_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    w.rest_until = None
    db.commit()
    return _show(request, db, user, w)


@router.post("/trening/{workout_id}/cw/{we_id}/usun", dependencies=[Depends(verify_csrf)])
def remove_exercise(workout_id: int, we_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    w.exercises.remove(_we(w, we_id))
    db.commit()
    return _go(f"/forma/trening/{w.id}")


@router.post("/trening/{workout_id}/koniec", dependencies=[Depends(verify_csrf)])
def finish(workout_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    if not tr.counts(w):
        db.delete(w)  # nothing done – no empty workout in the history
        db.commit()
        return _go("/forma")
    tr.finish(db, w)
    db.commit()
    return _go(f"/forma/trening/{w.id}")


@router.post("/trening/{workout_id}/wznow", dependencies=[Depends(verify_csrf)])
def reopen(workout_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    w.finished_at = None
    db.commit()
    return _go(f"/forma/trening/{w.id}")


@router.post("/trening/{workout_id}/usun", dependencies=[Depends(verify_csrf)])
def delete(workout_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(_workout(db, user, workout_id))
    db.commit()
    return _go("/forma")


# ---------------------------------------------------------------- picking an exercise (swap / add)


def _groups(db: Session, allowed_eq: set[str] | None, muscle: str | None) -> list[tuple[str, list[Exercise]]]:
    """All exercises for the "add" list: by pattern, filtered by muscle and equipment."""
    by: dict[str, list[Exercise]] = {}
    for ex in tr.exercises(db):
        if allowed_eq is not None and ex.equipment not in allowed_eq:
            continue
        if muscle and ex.muscle != muscle:
            continue
        by.setdefault(ex.pattern, []).append(ex)
    order = list(tr.catalog()["patterns"])
    return [(tr.pattern_label(p), by[p]) for p in sorted(by, key=lambda p: order.index(p) if p in order else 99)]


def _pick_ctx(user: User, **kw) -> dict:
    return {
        "user": user,
        "equipment_label": tr.equipment_label,
        "muscles": tr.catalog()["muscles"],
        "pattern_label": tr.pattern_label,
        "muscle_label": tr.muscle_label,
    } | kw


@router.get("/trening/{workout_id}/cw/{we_id}/zamien")
def swap_page(
    workout_id: int,
    we_id: int,
    request: Request,
    wszystko: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    w = _workout(db, user, workout_id)
    we = _we(w, we_id)
    eq = None if wszystko else tr.allowed(w)
    same, muscle = tr.substitutes(db, we.exercise, eq)
    return render(
        request,
        "exercise_pick.html",
        _pick_ctx(
            user,
            title=f"Zamiast: {we.exercise.name}",
            back=f"/forma/trening/{w.id}#we{we.id}",
            action=f"/forma/trening/{w.id}/cw/{we.id}/zamien/",
            sections=[
                (f"Ten sam ruch – {tr.pattern_label(we.exercise.pattern).lower()}", same),
                (f"Ta sama partia – {tr.muscle_label(we.exercise.muscle).lower()}", muscle),
            ],
            filtered=eq is not None,
            all_url=f"/forma/trening/{w.id}/cw/{we.id}/zamien?wszystko=1",
        ),
    )


@router.post("/trening/{workout_id}/cw/{we_id}/zamien/{exercise_id}", dependencies=[Depends(verify_csrf)])
def swap(
    workout_id: int, we_id: int, exercise_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    w = _workout(db, user, workout_id)
    we = _we(w, we_id)
    tr.swap(db, we, _exercise(db, exercise_id))
    db.commit()
    return _go(f"/forma/trening/{w.id}#we{we.id}")


@router.get("/trening/{workout_id}/dodaj")
def add_page(
    workout_id: int,
    request: Request,
    partia: str = "",
    wszystko: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    w = _workout(db, user, workout_id)
    eq = None if wszystko else tr.allowed(w)
    base = f"/forma/trening/{w.id}/dodaj"
    return render(
        request,
        "exercise_pick.html",
        _pick_ctx(
            user,
            title="Dodaj ćwiczenie",
            back=f"/forma/trening/{w.id}",
            action=f"{base}/",
            sections=_groups(db, eq, partia or None),
            muscle_filter=partia,
            filter_url=base + ("?wszystko=1&" if wszystko else "?"),
            filtered=eq is not None,
            all_url=f"{base}?wszystko=1" + (f"&partia={partia}" if partia else ""),
        ),
    )


@router.post("/trening/{workout_id}/dodaj/{exercise_id}", dependencies=[Depends(verify_csrf)])
def add(workout_id: int, exercise_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    we = tr.add_exercise(db, w, _exercise(db, exercise_id))
    db.commit()
    return _go(f"/forma/trening/{w.id}#we{we.id}")


# ---------------------------------------------------------------- progress


@router.get("/postepy")
def progress(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "training_progress.html", {"user": user, "rows": tr.done_exercises(db, user)})


@router.get("/postepy/{exercise_id}")
def exercise_history(
    exercise_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    ex = _exercise(db, exercise_id)
    rows = tr.history(db, user, ex.id, limit=40)
    return render(
        request,
        "training_exercise.html",
        {"user": user, "ex": ex, "rows": rows, "fmt_kg": tr.fmt_kg, "chart": _chart(rows), "tr": tr},
    )


def _chart(rows: list[tr.HistoryRow]) -> dict | None:
    """Points of the best estimated 1RM per session (oldest → newest) for a small inline SVG."""
    pts = [r for r in reversed(rows) if r.best > 0]
    if len(pts) < 2:
        return None
    lo, hi = min(r.best for r in pts), max(r.best for r in pts)
    span = (hi - lo) or 1
    w, h = 300, 80
    xy = [
        (round(i * w / (len(pts) - 1), 1), round(h - 8 - (r.best - lo) / span * (h - 16), 1)) for i, r in enumerate(pts)
    ]
    return {"w": w, "h": h, "points": " ".join(f"{x},{y}" for x, y in xy), "last": xy[-1], "lo": lo, "hi": hi}


# ---------------------------------------------------------------- plans and the weekly goal


@router.get("/plany")
def plans_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    tr.ensure_library(db)
    tr.seed_plans(db)
    db.commit()
    return render(request, "training_plans.html", {"user": user, "plans": tr.plans(db), "prefs": tr.prefs(db, user)})


@router.post("/plany", dependencies=[Depends(verify_csrf)])
async def new_plan(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    name = str(form.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Podaj nazwę planu")
    top = max((p.position for p in tr.plans(db)), default=-1)
    p = WorkoutPlan(name=name, position=top + 1)
    db.add(p)
    db.commit()
    return _go(f"/forma/plany/{p.id}")


@router.get("/plany/{plan_id}")
def plan_page(plan_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "training_plan.html", {"user": user, "p": _plan(db, plan_id)})


@router.post("/plany/{plan_id}", dependencies=[Depends(verify_csrf)])
async def save_plan(plan_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The whole plan in one form: name and every item's sets / range / seconds / rest / superset."""
    p = _plan(db, plan_id)
    form = await request.form()
    p.name = str(form.get("name") or p.name).strip()[:60] or p.name

    def num(key: str, lo: int, hi: int) -> int | None:
        raw = str(form.get(key) or "").strip()
        if not raw:
            return None
        v = int(raw)
        if not lo <= v <= hi:
            raise ValueError(f"Wartość spoza zakresu {lo}–{hi}")
        return v

    for it in p.items:
        k = f"i{it.id}_"
        it.sets = num(k + "sets", 1, 20) or it.sets
        it.reps_min, it.reps_max = num(k + "reps_min", 1, 100), num(k + "reps_max", 1, 100)
        if it.reps_min and it.reps_max and it.reps_min > it.reps_max:
            it.reps_min, it.reps_max = it.reps_max, it.reps_min
        it.seconds = num(k + "seconds", 1, 3600)
        it.rest_seconds = num(k + "rest", 0, 900) or 0
        it.superset = num(k + "superset", 1, 9)
    db.commit()
    return _go(f"/forma/plany/{p.id}")


@router.post("/plany/{plan_id}/cw/{item_id}/{action}", dependencies=[Depends(verify_csrf)])
def plan_item_action(
    plan_id: int, item_id: int, action: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    p = _plan(db, plan_id)
    it = db.get(PlanItem, item_id)
    if it is None or it.plan_id != p.id or action not in ("gora", "dol", "usun"):
        raise HTTPException(404, "Nie ma takiej pozycji")
    items = list(p.items)
    i = items.index(it)
    if action == "usun":
        p.items.remove(it)
    elif action == "gora" and i > 0:
        items[i - 1], items[i] = items[i], items[i - 1]
    elif action == "dol" and i < len(items) - 1:
        items[i + 1], items[i] = items[i], items[i + 1]
    for pos, x in enumerate(x for x in items if x in p.items):
        x.position = pos
    db.commit()
    return _go(f"/forma/plany/{p.id}#i{item_id}")


@router.get("/plany/{plan_id}/dodaj")
def plan_add_page(
    plan_id: int, request: Request, partia: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)
):
    p = _plan(db, plan_id)
    base = f"/forma/plany/{p.id}/dodaj"
    return render(
        request,
        "exercise_pick.html",
        _pick_ctx(
            user,
            title=f"Dodaj do: {p.name}",
            back=f"/forma/plany/{p.id}",
            action=f"{base}/",
            sections=_groups(db, None, partia or None),
            muscle_filter=partia,
            filter_url=base + "?",
            filtered=False,
            all_url=None,
        ),
    )


@router.post("/plany/{plan_id}/dodaj/{exercise_id}", dependencies=[Depends(verify_csrf)])
def plan_add(plan_id: int, exercise_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = _plan(db, plan_id)
    ex = _exercise(db, exercise_id)
    top = max((i.position for i in p.items), default=-1)
    timed = ex.kind == "time"
    it = PlanItem(
        exercise_id=ex.id,
        position=top + 1,
        sets=3,
        reps_min=None if timed else 8,
        reps_max=None if timed else 12,
        seconds=30 if timed else None,
        rest_seconds=tr.DEFAULT_REST,
    )
    p.items.append(it)
    db.commit()
    return _go(f"/forma/plany/{p.id}#i{it.id}")


@router.post("/plany/{plan_id}/archiwum", dependencies=[Depends(verify_csrf)])
def archive_plan(plan_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _plan(db, plan_id).archived = True
    db.commit()
    return _go("/forma/plany")


@router.post("/cel", dependencies=[Depends(verify_csrf)])
async def save_goal(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    p = tr.prefs(db, user)
    goal, gap = int(str(form.get("weekly_goal") or 3)), int(str(form.get("gap_days") or 3))
    if not (1 <= goal <= 7 and 1 <= gap <= 14):
        raise ValueError("Cel: 1–7 treningów, przypomnienie po 1–14 dniach")
    p.weekly_goal, p.gap_days = goal, gap
    db.commit()
    return _go("/forma/plany")
