"""/obowiazki – house chores by urgency, with "Teraz: <osoba>" and ✓ that passes the turn on."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import chores, points, services, timers
from domownik.config import today
from domownik.db import get_db
from domownik.models import Chore, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter()

MAX_EVERY_DAYS = 730


def _back(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _list_url(who, anchor: int | None = None) -> str:
    url = "/obowiazki" + ("?who=me" if who == "me" else "")
    return url + (f"#ch{anchor}" if anchor else "")


def _error(msg: str, target: str) -> HTMLResponse:
    return HTMLResponse(
        f'<p class="error">{msg}</p>',
        status_code=422,
        headers={"HX-Retarget": target, "HX-Reswap": "innerHTML"},
    )


@router.get("/obowiazki")
def chores_page(
    request: Request, who: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    t = today()
    users = services.active_users(db)
    sts = chores.states(db, t, user_id=user.id if who == "me" else None)
    rooms = sorted({r for r in db.scalars(select(Chore.room)) if r})
    return render(
        request,
        "chores.html",
        {
            "user": user,
            "states": sts,
            "who": who if who == "me" else None,
            "users": users,
            "rooms": rooms,
            "suggestions": chores.missing_suggestions(db),
            "has_any": db.scalar(select(Chore.id).limit(1)) is not None,
            "overdue_n": sum(1 for s in sts if s.overdue),
            "running": {t.chore_id: t for t in timers.active(db) if t.chore_id},
            "timer_choices": {s.chore.id: _timer_choices(db, s.chore) for s in sts if s.chore.uses_timer},
            "duration_text": timers.duration_text,
        },
    )


def _timer_choices(db: Session, chore: Chore) -> list[tuple[int, bool]]:
    """⏱ buttons of a chore: 1/2/3 h plus the time used last, which is the highlighted one."""
    last = timers.last_minutes(db, chore) or timers.PRESETS[0]
    return [(m, m == last) for m in sorted({*timers.PRESETS, last})]


def _int(raw, lo: int, hi: int, msg: str, *, required: bool = True) -> int | None:
    raw = str(raw or "").strip()
    if not raw:
        if required:
            raise ValueError(msg)
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(msg) from None
    if not lo <= value <= hi:
        raise ValueError(msg)
    return value


def _rotation(db: Session, form, me: User) -> list[int]:
    """Checked people in a stable order (account order), or everyone when the field is absent."""
    valid = [u.id for u in services.active_users(db)]
    if "rotation_sent" not in form:
        return [me.id] + [u for u in valid if u != me.id]
    picked = {int(x) for x in form.getlist("rotation") if str(x).isdigit()}
    return [u for u in valid if u in picked]


def _apply(db: Session, chore: Chore, form, me: User) -> None:
    name = str(form.get("name") or "").strip()[:120]
    if not name:
        raise ValueError("Podaj nazwę, np. Odkurzanie")
    chore.name = name
    chore.every_days = _int(form.get("every_days"), 1, MAX_EVERY_DAYS, f"Co ile dni: liczba 1–{MAX_EVERY_DAYS}")
    chore.room = str(form.get("room") or "").strip()[:60] or None
    chore.est_minutes = _int(form.get("est_minutes"), 1, 1440, "Czas w minutach: 1–1440", required=False)
    if "points" in form:
        chore.points = points.parse_points(form.get("points"))
        chore.needs_approval = bool(form.get("needs_approval")) and bool(chore.points)
    if "timer_sent" in form:
        chore.uses_timer = bool(form.get("uses_timer"))
    turn = str(form.get("turn") or "").strip()
    chores.set_rotation(chore, _rotation(db, form, me), int(turn) if turn.isdigit() else None)


@router.post("/obowiazki", dependencies=[Depends(verify_csrf)])
async def add_chore(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    chore = Chore(created_by_id=user.id, rotation=[])
    try:
        _apply(db, chore, form, user)
    except ValueError as exc:
        return _error(str(exc), "#chore-error-new")
    db.add(chore)
    db.commit()
    return _back(_list_url(form.get("who"), chore.id))


@router.post("/obowiazki/propozycja", dependencies=[Depends(verify_csrf)])
async def add_suggestion(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """One click: a ready chore (interval, room, minutes) taking turns among everyone, me first."""
    form = await request.form()
    pick = next((s for s in chores.SUGGESTIONS if s[0] == form.get("name")), None)
    if pick is None:
        raise HTTPException(400, "Nie ma takiej propozycji")
    name, room, every, minutes = pick
    existing = db.scalar(select(Chore).where(Chore.name == name))
    if existing is None:
        existing = Chore(
            name=name,
            room=room,
            every_days=every,
            est_minutes=minutes,
            created_by_id=user.id,
            uses_timer=name in chores.TIMER_SUGGESTIONS,
        )
        chores.set_rotation(existing, _rotation(db, {}, user))
        db.add(existing)
        db.commit()
    return _back(_list_url(form.get("who"), existing.id))


def _get(db: Session, chore_id: int) -> Chore:
    chore = db.get(Chore, chore_id)
    if chore is None:
        raise HTTPException(404, "Nie ma takiego obowiązku")
    return chore


@router.post("/obowiazki/{chore_id}", dependencies=[Depends(verify_csrf)])
async def edit_chore(
    request: Request, chore_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    chore = _get(db, chore_id)
    form = await request.form()
    try:
        _apply(db, chore, form, user)
    except ValueError as exc:
        db.rollback()
        return _error(str(exc), f"#chore-error-{chore_id}")
    db.commit()
    return _back(_list_url(form.get("who"), chore.id))


@router.post("/obowiazki/{chore_id}/zrobione", dependencies=[Depends(verify_csrf)])
async def done_chore(
    request: Request, chore_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """✓ – by me, or (field `by`) for someone else."""
    chore = _get(db, chore_id)
    form = await request.form()
    by = user
    raw = str(form.get("by") or "").strip()
    if raw.isdigit():
        other = db.get(User, int(raw))
        if other is None or not other.is_active:
            raise HTTPException(400, "Nie ma takiej osoby")
        by = other
    rec = chores.complete(db, chore, by=by, on=today())
    points.for_chore(db, chore, rec, by, user)
    db.commit()
    return _back(_list_url(form.get("who"), chore.id))


@router.post("/obowiazki/{chore_id}/cofnij", dependencies=[Depends(verify_csrf)])
async def undo_chore(
    request: Request, chore_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    chore = _get(db, chore_id)
    form = await request.form()
    chores.undo_last(db, chore)
    db.commit()
    return _back(_list_url(form.get("who"), chore.id))


@router.post("/obowiazki/{chore_id}/usun", dependencies=[Depends(verify_csrf)])
async def delete_chore(
    request: Request, chore_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    form = await request.form()
    db.delete(_get(db, chore_id))
    db.commit()
    return _back(_list_url(form.get("who")))
