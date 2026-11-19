"""🏋️ Training in "Forma": start, the workout screen on the phone, substitutes, plans, progress.

Workouts are private (another person's id is a 404); the exercise library and the plans are shared.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from domownik import stretching as st
from domownik import together
from domownik import training as tr
from domownik.config import get_settings, now, today
from domownik.db import get_db
from domownik.models import (
    Exercise,
    PlanItem,
    User,
    Workout,
    WorkoutExercise,
    WorkoutGroup,
    WorkoutPlan,
    WorkoutSet,
)
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
    if tr.drop_stale_empty(db, user, t):
        db.commit()
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
        "recent": _recent(db, user),
        "stretch_recent": st.recent(db, user, 3),
        "st": st,
        "today": t,
        "invites": together.open_invites(db, user, t),
        "stretch_invites": st.open_invites(db, user),
        "partners": together.partners(db, user),
    }


def _recent(db: Session, user: User, limit: int = 4) -> list[Workout]:
    """Finished workouts, newest first (the stretching sessions are listed under 🤸)."""
    return [w for w in tr.mine(db, user, limit=limit + 3) if w.finished_at is not None][:limit]


# ---------------------------------------------------------------- start


def _parse_day(raw) -> date | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    d = date.fromisoformat(raw)
    if d > today():
        raise ValueError("Trening z przyszłości? Wybierz dzisiejszy albo wcześniejszy dzień")
    return d


@router.get("/trening/nowy")
def start_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    tr.ensure_library(db)
    tr.seed_plans(db)
    db.commit()
    return render(
        request,
        "workout_start.html",
        {
            "user": user,
            "plans": tr.plans(db),
            "next_plan": tr.next_plan(db, user),
            "today": today(),
            "away_labels": [tr.equipment_label(e).lower() for e in tr.away_equipment()],
            "partners": together.partners(db, user),
        },
    )


@router.post("/trening", dependencies=[Depends(verify_csrf)])
async def start(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    raw_plan = str(form.get("plan_id") or "")
    plan = _plan(db, int(raw_plan)) if raw_plan.isdigit() else None
    day = _parse_day(form.get("day"))
    running = tr.active(db, user)
    if running is not None and (day is None or day == today()) and running.day == today():
        return _go(f"/forma/trening/{running.id}")  # one going today already – back to it
    w = tr.start(db, user, plan, day=day, away=form.get("where") == "away" or bool(form.get("away")))
    invitees = _invitees(db, user, form)
    if invitees and w.day == today():
        db.commit()
        together.invite(db, w, invitees)
    db.commit()
    return _go(f"/forma/trening/{w.id}")


def _invitees(db: Session, user: User, form) -> list[User]:
    ids = {str(v) for v in form.getlist("razem")}
    return [u for u in together.partners(db, user) if str(u.id) in ids]


# ---------------------------------------------------------------- the workout screen


def _screen(db: Session, user: User, w: Workout, errors: dict | None = None) -> dict:
    tz = get_settings().tz
    nxt = tr.next_up(w)
    recs = tr.records(w, tr.prior_best(db, w))
    rows = []
    for we in w.exercises:
        sug = tr.suggest(db, user, we)
        last_done = we.work[-1] if we.work else None  # a warm-up set does not set the next one's numbers
        rows.append(
            {
                "we": we,
                "sug": sug,
                "weight": last_done.weight if last_done and last_done.weight is not None else sug.weight,
                "reps": sug.reps if not last_done else (last_done.reps or sug.reps),
                "seconds": last_done.seconds if last_done and last_done.seconds else sug.seconds,
                "complete": len(we.work) >= we.sets,
                "current": nxt is not None and nxt.id == we.id,
                "record": recs.get(we.id),
                "sets_text": tr.sets_text(we.work),
                # 🔗 with the next one (not already together); ✂ on the last of a superset
                "can_link": (f := tr.following(w, we)) is not None
                and (we.superset is None or f.superset != we.superset),
                "ss_last": we.superset is not None and (f is None or f.superset != we.superset),
                # ⏱ the rest after it is this one's (inside a superset: the last one's) – editable in place
                "rest_here": tr.rest_holder(we),
                "plan_rest": (it.rest_seconds if (it := tr.plan_item_for(we)) is not None else None),
            }
        )
    rest_ms = None
    stale = now().replace(tzinfo=None) - timedelta(minutes=10)
    if w.rest_until is not None and w.finished_at is None and w.rest_until > stale:  # not a rest from hours ago
        rest_ms = int(w.rest_until.replace(tzinfo=tz).timestamp() * 1000)
    total = sum(we.sets for we in w.exercises)
    done = sum(min(len(we.work), we.sets) for we in w.exercises)
    # ⇉ a superset at the start of a round: one form for all its exercises (you did both, then log)
    rounds = {g: ms for g in {we.superset for we in w.exercises} if (ms := tr.round_members(w, g))}
    return {
        "user": user,
        "w": w,
        "rows": rows,
        "rest_ms": rest_ms,
        "next_we": nxt,
        "progress": (done, total),
        "summary": tr.summary(db, w, recs) if w.finished_at else None,
        "rounds": rounds,
        "row_of": {r["we"].id: r for r in rows},
        "stretch": st.offer(db, user, w, done),
        "st": st,
        "errors": errors or {},
        "fmt_kg": tr.fmt_kg,
        "set_text": tr.set_text,
        "superset_letter": lambda n: "ABCDEFGH"[(n - 1) % 8] if n else "",
    } | _together(db, user, w)


def _together(db: Session, user: User, w: Workout) -> dict:
    views = together.views(w)
    live = w.finished_at is None and w.day == today()
    return {
        "partner_views": views,
        "partner_slots": [(v.workout_id, v.name, v.slots) for v in views],
        "waiting": together.waiting_for(db, w) if live else [],
        "can_invite": [u for u in together.partners(db, user) if u.id not in (w.group.invited if w.group else [])]
        if live
        else [],
        "sig": together.signature(w),
        "live_together": live and w.group is not None,
        # 👫 someone started another training and invited this person: one tap moves over to it
        "switch_to": [(g, host) for g, host in together.open_invites(db, user, today()) if g.id != w.group_id]
        if live
        else [],
        "poll": live and (w.group is not None or bool(together.partners(db, user))),
    }


def _show(request: Request, db: Session, user: User, w: Workout, errors: dict | None = None, **extra):
    ctx = _screen(db, user, w, errors) | extra
    if is_htmx(request) and request.headers.get("hx-target") == "workout":
        return render(request, "partials/workout_body.html", ctx)
    return render(request, "workout.html", ctx)


@router.get("/trening/{workout_id:int}")
def workout_page(workout_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _show(request, db, user, _workout(db, user, workout_id))


@router.get("/trening/{workout_id:int}/razem")
def partner_strip(workout_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """👫 polled every few seconds: how far the partner is (counts only) + the signature of one's own list."""
    w = tr.get_own(db, user, workout_id)
    if w is None:  # removed meanwhile (the empty one, when the partner deleted the training) – back to Forma
        return HTMLResponse("", headers={"HX-Redirect": "/forma"})
    return render(request, "partials/workout_partner.html", {"user": user, "w": w} | _together(db, user, w))


@router.post("/trening/{workout_id:int}/zapros", dependencies=[Depends(verify_csrf)])
async def invite(workout_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    form = await request.form()
    invitees = _invitees(db, user, form)
    if invitees and w.finished_at is None and w.day == today():
        together.invite(db, w, invitees)
    db.commit()
    return _go(f"/forma/trening/{w.id}")


def _join(request: Request, db: Session, user: User, group_id: int, replace: bool):
    g = db.get(WorkoutGroup, group_id)
    if g is None:
        raise HTTPException(404, "Nie ma takiego treningu")
    try:
        w = together.join(db, g, user, today=today(), at=now().replace(tzinfo=None), replace=replace)
    except together.Busy as busy:
        db.rollback()
        return render(request, "workout_join.html", {"user": user, "g": g, "own": busy.workout, "error": ""})
    except ValueError as exc:
        db.rollback()
        return render(request, "workout_join.html", {"user": user, "g": g, "own": None, "error": str(exc)})
    db.commit()
    return _go(f"/forma/trening/{w.id}")


@router.get("/razem/{group_id:int}")
def join(group_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The link in the invitation push: opening it joins (one tap). Harmless to open twice."""
    return _join(request, db, user, group_id, replace=False)


@router.post("/razem/{group_id:int}/dolacz", dependencies=[Depends(verify_csrf)])
def join_finishing_own(
    group_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return _join(request, db, user, group_id, replace=True)


def _marks(form, suffix: str = "") -> dict:
    """🌡 / 🔥 / ↘ of one set from the form (the round form: one suffix per exercise)."""
    return {
        "warmup": bool(form.get(f"warmup{suffix}")),
        "failure": bool(form.get(f"failure{suffix}")),
        "drops": tr.drop_rows(form, suffix) if form.get(f"drop{suffix}") else None,
    }


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


@router.post("/trening/{workout_id:int}/cw/{we_id:int}/seria", dependencies=[Depends(verify_csrf)])
async def log_set(
    workout_id: int, we_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    w = _workout(db, user, workout_id)
    we = _we(w, we_id)
    form = await request.form()
    try:
        weight = _num(form.get("weight"), label="Ciężar")
        marks = _marks(form)
        if we.exercise.kind == "time":
            seconds = _num(form.get("seconds"), integer=True, label="Czas")
            if not seconds:
                raise ValueError("Wpisz czas w sekundach")
            tr.log_set(db, we, weight=weight, reps=None, seconds=seconds, **marks)
        else:
            reps = _num(form.get("reps"), integer=True, label="Powtórzenia")
            if not reps:
                raise ValueError("Wpisz liczbę powtórzeń")
            tr.log_set(db, we, weight=weight, reps=reps, **marks)
    except ValueError as exc:
        return _show(request, db, user, w, {we.id: str(exc)})
    db.commit()
    return _show(request, db, user, w)


@router.post("/trening/{workout_id:int}/runda/{group:int}", dependencies=[Depends(verify_csrf)])
async def log_round(
    workout_id: int, group: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """⇉ One set of each exercise of the superset at once – the rest starts after them."""
    w = _workout(db, user, workout_id)
    members = tr.round_members(w, group)
    if not members:
        return _show(request, db, user, w)
    form = await request.form()
    entries = []
    try:
        for we in members:
            weight = _num(form.get(f"weight_{we.id}"), label=f"{we.exercise.name} – ciężar")
            if we.exercise.kind == "time":
                seconds = _num(form.get(f"seconds_{we.id}"), integer=True, label=f"{we.exercise.name} – czas")
                if not seconds:
                    raise ValueError(f"{we.exercise.name}: wpisz czas w sekundach")
                entries.append((we, weight, None, seconds, _marks(form, f"_{we.id}")))
            else:
                reps = _num(form.get(f"reps_{we.id}"), integer=True, label=f"{we.exercise.name} – powtórzenia")
                if not reps:
                    raise ValueError(f"{we.exercise.name}: wpisz liczbę powtórzeń")
                entries.append((we, weight, reps, None, _marks(form, f"_{we.id}")))
    except ValueError as exc:
        return _show(request, db, user, w, {f"round{group}": str(exc)})
    at = now().replace(tzinfo=None)
    for i, (we, weight, reps, seconds, marks) in enumerate(entries):
        tr.log_set(db, we, weight=weight, reps=reps, seconds=seconds, at=at + timedelta(seconds=i), **marks)
    db.commit()
    return _show(request, db, user, w)


@router.post("/trening/{workout_id:int}/plan", dependencies=[Depends(verify_csrf)])
async def save_as_plan(
    workout_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """💾 The workout made up on the spot becomes a plan to start next time."""
    w = _workout(db, user, workout_id)
    if w.plan_id is not None or not w.exercises:
        return _go(f"/forma/trening/{w.id}")
    form = await request.form()
    p = tr.save_as_plan(db, w, str(form.get("name") or ""))
    db.commit()
    return _go(f"/forma/plany/{p.id}")


@router.post("/trening/{workout_id:int}/seria/{set_id:int}/usun", dependencies=[Depends(verify_csrf)])
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


@router.post("/trening/{workout_id:int}/przerwa-koniec", dependencies=[Depends(verify_csrf)])
def skip_rest(workout_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    w.rest_until = None
    db.commit()
    return _show(request, db, user, w)


@router.post("/trening/{workout_id:int}/przerwa", dependencies=[Depends(verify_csrf)])
def change_rest(
    workout_id: int,
    request: Request,
    o: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """−15 s / +15 s on the rest bar."""
    w = _workout(db, user, workout_id)
    if o in (-tr.REST_STEP, tr.REST_STEP):
        tr.adjust_rest(db, w, o)
        db.commit()
    return _show(request, db, user, w)


@router.post("/trening/{workout_id:int}/cw/{we_id:int}/przerwa", dependencies=[Depends(verify_csrf)])
def change_exercise_rest(
    workout_id: int,
    we_id: int,
    request: Request,
    o: int = 0,
    plan: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """⏱ −15 s / +15 s on one exercise during the workout; plan=1 (📌) also into the plan."""
    w = _workout(db, user, workout_id)
    we = _we(w, we_id)
    if w.finished_at is None and (o in (-tr.REST_STEP, tr.REST_STEP) or (o == 0 and plan)):
        tr.set_exercise_rest(db, we, o, to_plan=bool(plan))
        db.commit()
    return _show(request, db, user, w, rest_open=we.id)  # stays open for the next tap


@router.post("/trening/{workout_id:int}/cw/{we_id:int}/superseria", dependencies=[Depends(verify_csrf)])
def link_superset(
    workout_id: int, we_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """🔗 This exercise and the next one into a superset."""
    w = _workout(db, user, workout_id)
    we = _we(w, we_id)
    if tr.following(w, we) is not None:
        together.link_superset(db, w, we)
        db.commit()
    return _show(request, db, user, w)


@router.post("/trening/{workout_id:int}/cw/{we_id:int}/rozlacz", dependencies=[Depends(verify_csrf)])
def unlink_superset(
    workout_id: int, we_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    w = _workout(db, user, workout_id)
    together.unlink_superset(db, w, _we(w, we_id))
    db.commit()
    return _show(request, db, user, w)


@router.post("/trening/{workout_id:int}/cw/{we_id:int}/usun", dependencies=[Depends(verify_csrf)])
def remove_exercise(workout_id: int, we_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    together.remove(db, w, _we(w, we_id))
    db.commit()
    return _go(f"/forma/trening/{w.id}")


@router.post("/trening/{workout_id:int}/koniec", dependencies=[Depends(verify_csrf)])
def finish(workout_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    if not tr.counts(w):
        db.delete(w)  # nothing done – no empty workout in the history
        db.commit()
        return _go("/forma")
    tr.finish(db, w)
    db.commit()
    return _go(f"/forma/trening/{w.id}")


@router.post("/trening/{workout_id:int}/wznow", dependencies=[Depends(verify_csrf)])
def reopen(workout_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    w.finished_at = None
    db.commit()
    return _go(f"/forma/trening/{w.id}")


@router.post("/trening/{workout_id:int}/usun", dependencies=[Depends(verify_csrf)])
def delete(workout_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    for o in together.others(w):  # who joined and did nothing yet would stay in a training with nobody
        if o.finished_at is None and not tr.counts(o):
            db.delete(o)
    db.delete(w)
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


@router.get("/trening/{workout_id:int}/cw/{we_id:int}/zamien")
def swap_page(
    workout_id: int,
    we_id: int,
    request: Request,
    wszystko: int = 0,
    dla: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    w = _workout(db, user, workout_id)
    we = _we(w, we_id)
    eq = None if wszystko else tr.allowed(w)
    shared = bool(together.others(w))
    dla = "ja" if dla == "ja" or not shared else "oboje"
    base = f"/forma/trening/{w.id}/cw/{we.id}/zamien"
    same, muscle = tr.substitutes(db, we.exercise, eq)
    return render(
        request,
        "exercise_pick.html",
        _pick_ctx(
            user,
            title=f"Zamiast: {we.exercise.name}",
            back=f"/forma/trening/{w.id}#we{we.id}",
            action=f"{base}/",
            action_query=f"?dla={dla}" if shared else "",
            for_whom=(dla, f"{base}?{'wszystko=1&' if wszystko else ''}dla=") if shared else None,
            partner_names=", ".join(o.user.name for o in together.others(w)),
            sections=[
                (f"Ten sam ruch – {tr.pattern_label(we.exercise.pattern).lower()}", same),
                (f"Ta sama partia – {tr.muscle_label(we.exercise.muscle).lower()}", muscle),
            ],
            filtered=eq is not None,
            all_url=f"/forma/trening/{w.id}/cw/{we.id}/zamien?wszystko=1",
        ),
    )


@router.post("/trening/{workout_id:int}/cw/{we_id:int}/zamien/{exercise_id:int}", dependencies=[Depends(verify_csrf)])
def swap(
    workout_id: int,
    we_id: int,
    exercise_id: int,
    dla: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    w = _workout(db, user, workout_id)
    we = _we(w, we_id)
    together.swap(db, w, we, _exercise(db, exercise_id), both=dla != "ja")
    db.commit()
    return _go(f"/forma/trening/{w.id}#we{we.id}")


@router.get("/trening/{workout_id:int}/dodaj")
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
    sections = _groups(db, eq, partia or None)
    if not partia:  # 🕘 what this person did lately, not in this workout yet – the usual pick is at the top
        have = {we.exercise_id for we in w.exercises}
        recent = [
            ex for ex, _, _ in tr.done_exercises(db, user) if ex.id not in have and (eq is None or ex.equipment in eq)
        ]
        sections = [("🕘 Ostatnio robione", recent[:8]), *sections]
    return render(
        request,
        "exercise_pick.html",
        _pick_ctx(
            user,
            title="Dodaj ćwiczenie",
            back=f"/forma/trening/{w.id}",
            action=f"{base}/",
            sections=sections,
            muscle_filter=partia,
            filter_url=base + ("?wszystko=1&" if wszystko else "?"),
            filtered=eq is not None,
            all_url=f"{base}?wszystko=1" + (f"&partia={partia}" if partia else ""),
        ),
    )


@router.post("/trening/{workout_id:int}/dodaj/{exercise_id:int}", dependencies=[Depends(verify_csrf)])
def add(workout_id: int, exercise_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _workout(db, user, workout_id)
    we = together.add(db, w, _exercise(db, exercise_id))
    db.commit()
    return _go(f"/forma/trening/{w.id}#we{we.id}")


# ---------------------------------------------------------------- progress


@router.get("/cwiczenie/{exercise_id:int}")
def exercise_guide(
    exercise_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """ⓘ How the exercise is done – in the modal (the workout's timer keeps going); without htmx its page."""
    ex = _exercise(db, exercise_id)
    if request.headers.get("hx-target") != "modal-body":
        return RedirectResponse(f"/forma/postepy/{ex.id}", status_code=303)
    return render(request, "partials/ex_guide_modal.html", _guide_ctx(user, ex))


def _guide_ctx(user: User, ex: Exercise) -> dict:
    return {
        "user": user,
        "ex": ex,
        "guide": tr.guide(ex),
        "img_start": tr.image(ex),
        "img_end": tr.image_end(ex),
        "tr": tr,
    }


@router.get("/postepy")
def progress(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = tr.done_exercises(db, user)
    cut = today() - timedelta(days=30)
    return render(
        request,
        "training_progress.html",
        {
            "user": user,
            "sections": [
                ("Ostatnie 30 dni", [r for r in rows if r[1] >= cut]),
                ("Wcześniej", [r for r in rows if r[1] < cut]),
            ],
            "any": bool(rows),
            "best": tr.best_e1rm(db, user),
            "fmt_kg": tr.fmt_kg,
            "muscle_label": tr.muscle_label,
            "equipment_label": tr.equipment_label,
        },
    )


RANGES = (("3m", "3 mies.", 91), ("1r", "Rok", 365), ("all", "Całość", None))


@router.get("/postepy/{exercise_id:int}")
def exercise_history(
    exercise_id: int,
    request: Request,
    okres: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    ex = _exercise(db, exercise_id)
    every = tr.history(db, user, ex.id, limit=None)
    t = today()
    if okres not in {k for k, _, _ in RANGES}:  # by default the last year – unless that would hide everything
        okres = "1r" if any(r.day >= t - timedelta(days=365) for r in every) else "all"
    days = next(d for k, _, d in RANGES if k == okres)
    rows = [r for r in every if days is None or r.day >= t - timedelta(days=days)]
    top = max((r.best for r in every), default=0)
    return render(
        request,
        "training_exercise.html",
        _guide_ctx(user, ex)
        | {
            "rows": rows,
            "fmt_kg": tr.fmt_kg,
            "chart": _chart(rows, top),
            "ranges": [
                (k, label) for k, label, d in RANGES if d is None or (every and every[-1].day < t - timedelta(days=d))
            ],
            "okres": okres,
            "top": top,
            "spans_more": len(rows) < len(every),
        },
    )


def _chart(rows: list[tr.HistoryRow], top: float = 0) -> dict | None:
    """The best estimated 1RM per session (oldest → newest) on a time axis, for a small inline SVG:
    min/max labels on the left, the first/last day below, 🏆 where the all-time best was."""
    pts = [r for r in reversed(rows) if r.best > 0]
    if len(pts) < 2:
        return None
    lo, hi = min(r.best for r in pts), max(r.best for r in pts)
    span = (hi - lo) or 1
    left, w, h = 34, 300, 90
    d0, d1 = pts[0].day, pts[-1].day
    days = (d1 - d0).days or 1

    def x(r):
        return round(left + (r.day - d0).days * (w - left) / days, 1)

    def y(v):
        return round(h - 10 - (v - lo) / span * (h - 22), 1)

    xy = [(x(r), y(r.best)) for r in pts]
    best = max(pts, key=lambda r: r.best)
    return {
        "w": w,
        "h": h,
        "left": left,
        "points": " ".join(f"{a},{b}" for a, b in xy),
        "last": xy[-1],
        "lo": lo,
        "hi": hi,
        "y_lo": y(lo),
        "y_hi": y(hi),
        "from": d0,
        "to": d1,
        "pr": (x(best), y(best.best)) if top and best.best >= top - 1e-9 else None,
    }


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


@router.get("/plany/{plan_id:int}")
def plan_page(plan_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "training_plan.html", {"user": user, "p": _plan(db, plan_id)})


@router.post("/plany/{plan_id:int}", dependencies=[Depends(verify_csrf)])
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


@router.post("/plany/{plan_id:int}/cw/{item_id:int}/{action}", dependencies=[Depends(verify_csrf)])
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


@router.get("/plany/{plan_id:int}/dodaj")
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


@router.post("/plany/{plan_id:int}/dodaj/{exercise_id:int}", dependencies=[Depends(verify_csrf)])
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
        rest_seconds=tr.suggested_rest(ex),
    )
    p.items.append(it)
    db.commit()
    return _go(f"/forma/plany/{p.id}#i{it.id}")


@router.post("/plany/{plan_id:int}/archiwum", dependencies=[Depends(verify_csrf)])
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
