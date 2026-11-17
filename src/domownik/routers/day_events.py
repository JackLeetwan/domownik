"""/kalendarz/dni – 📌 all-day entries of the family calendar (holidays at whose family, trips, school breaks)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from domownik import dayevents, services
from domownik.config import today
from domownik.db import get_db
from domownik.models import DayEvent, User
from domownik.security import current_user, verify_csrf
from domownik.web import error_target, is_quick, quick_saved, render

router = APIRouter(prefix="/kalendarz/dni")

MAX_DAYS = 62  # a longer "day" is a mistake in the dates, not a trip


def _back(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _error(request: Request, msg: str) -> HTMLResponse:
    return HTMLResponse(
        f'<p class="error">{msg}</p>',
        status_code=422,
        headers={"HX-Retarget": error_target(request, "#dayev-error"), "HX-Reswap": "innerHTML"},
    )


def _get(db: Session, ev_id: int) -> DayEvent:
    ev = db.get(DayEvent, ev_id)
    if ev is None:
        raise HTTPException(404, "Nie ma takiego wpisu w kalendarzu")
    return ev


def _date(raw) -> date | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError("Zła data") from None


def apply_form(db: Session, ev: DayEvent, form) -> None:
    """Fill the entry from the form; ValueError with a Polish message on bad input."""
    title = str(form.get("title") or "").strip()[:120]
    if not title:
        raise ValueError("Podaj nazwę, np. Wigilia – Wschowa")
    starts_on = _date(form.get("starts_on"))
    if starts_on is None:
        raise ValueError("Podaj dzień")
    ends_on = _date(form.get("ends_on")) or starts_on
    if ends_on < starts_on:
        raise ValueError("Ostatni dzień jest wcześniejszy niż pierwszy")
    if (ends_on - starts_on).days >= MAX_DAYS:
        raise ValueError(f"Najwyżej {MAX_DAYS} dni – sprawdź daty")
    icon = str(form.get("icon") or "").strip()[:8] or "📌"
    raw_user = str(form.get("user_id") or "").strip()
    valid = {u.id for u in services.active_users(db)}
    ev.title, ev.icon = title, icon
    ev.starts_on, ev.ends_on = starts_on, ends_on
    ev.yearly = bool(form.get("yearly"))
    ev.user_id = int(raw_user) if raw_user.isdigit() and int(raw_user) in valid else None
    ev.notify = bool(form.get("notify"))
    ev.notes = str(form.get("notes") or "").strip()[:2000] or None


def _form_ctx(db: Session, user: User, ev: DayEvent | None, start: date) -> dict:
    return {"user": user, "ev": ev, "users": services.active_users(db), "icons": dayevents.ICONS, "start": start}


def _cal_url(d: date) -> str:
    return f"/kalendarz?m={d.strftime('%Y-%m')}&d={d.isoformat()}"


@router.get("")
def list_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = today()
    users = {u.id: u for u in services.active_users(db)}
    return render(request, "day_events.html", {"user": user, "rows": dayevents.upcoming(db, t), "users": users, "t": t})


@router.get("/nowy")
def new_page(
    request: Request, d: date | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return render(request, "day_event_form.html", _form_ctx(db, user, None, d or today()))


@router.get("/{ev_id}")
def edit_page(request: Request, ev_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = _get(db, ev_id)
    return render(request, "day_event_form.html", _form_ctx(db, user, ev, ev.starts_on))


@router.post("", dependencies=[Depends(verify_csrf)])
async def create(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    ev = DayEvent(created_by_id=user.id)
    try:
        apply_form(db, ev, form)
    except ValueError as exc:
        return _error(request, str(exc))
    db.add(ev)
    db.commit()
    if is_quick(request):
        return quick_saved(f"{ev.icon} {ev.title} – w kalendarzu")
    return _back(_cal_url(ev.starts_on))


@router.post("/{ev_id}", dependencies=[Depends(verify_csrf)])
async def update(request: Request, ev_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = _get(db, ev_id)
    form = await request.form()
    try:
        apply_form(db, ev, form)
    except ValueError as exc:
        db.rollback()
        return _error(request, str(exc))
    db.commit()
    return _back(_cal_url(ev.starts_on))


@router.post("/{ev_id}/usun", dependencies=[Depends(verify_csrf)])
def delete(ev_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = _get(db, ev_id)
    back = _cal_url(ev.starts_on)
    db.delete(ev)
    db.commit()
    return _back(back)
