"""/kalendarz/zewnetrzne – calendars from outside (kindergarten, school, work) by their ICS link."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik import extcal, services
from domownik.config import now
from domownik.db import get_db
from domownik.models import ExternalCalendar, ExternalEvent, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/kalendarz/zewnetrzne")

ICONS = ("📅", "🏫", "🧸", "💼", "⚽", "🎵", "🏥", "🎉")


def _short(url: str) -> str:
    """The host only – the rest of a calendar link is usually its secret."""
    return (urlsplit(url).hostname or url)[:60] + "/…"


def _page(request: Request, db: Session, user: User, **ctx):
    cals = extcal.visible_calendars(db)
    counts = dict(
        db.execute(
            select(ExternalEvent.calendar_id, func.count(ExternalEvent.id)).group_by(ExternalEvent.calendar_id)
        ).all()
    )
    return render(
        request,
        "calendar_external.html",
        {
            "user": user,
            "cals": cals,
            "counts": counts,
            "short": _short,
            "icons": ICONS,
            "users": services.active_users(db),
        }
        | ctx,
    )


def _get(db: Session, cal_id: int) -> ExternalCalendar:
    cal = db.get(ExternalCalendar, cal_id)
    if cal is None:
        raise HTTPException(404, "Nie ma takiego kalendarza")
    return cal


@router.get("")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _page(request, db, user)


@router.post("", dependencies=[Depends(verify_csrf)])
async def add(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    name = str(form.get("name") or "").strip()[:80]
    icon = str(form.get("icon") or "📅")
    owner = str(form.get("owner") or "")
    private = bool(form.get("private"))
    values = {"name": name, "url": str(form.get("url") or "").strip(), "icon": icon, "owner": owner}
    if not name:
        return _page(request, db, user, error="Podaj nazwę, np. „Przedszkole”.", values=values)
    try:
        url = extcal.normalize_url(values["url"])
    except extcal.CalendarError as exc:
        return _page(request, db, user, error=str(exc), values=values)
    if len(url) > 1000:
        return _page(request, db, user, error="Link jest za długi.", values=values)
    owner_id = int(owner) if owner.isdigit() and db.get(User, int(owner)) is not None else None
    if private:
        owner_id = user.id  # a private calendar is always one's own
    cal = ExternalCalendar(
        name=name,
        url=url,
        icon=icon if icon in ICONS else "📅",
        user_id=owner_id,
        private_to_id=user.id if private else None,
        notify=bool(form.get("notify")),
    )
    db.add(cal)
    db.commit()
    n = await run_in_threadpool(extcal.refresh, db, cal, now())  # the fetch must not block other requests
    if n < 0:
        return _page(request, db, user, error=f"Dodano, ale: {cal.error}", added=cal.id)
    return RedirectResponse(f"/kalendarz/zewnetrzne?ok={cal.id}", status_code=303)


@router.post("/{cal_id}/odswiez", dependencies=[Depends(verify_csrf)])
def refresh(cal_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    extcal.refresh(db, _get(db, cal_id), now())
    return RedirectResponse(f"/kalendarz/zewnetrzne?ok={cal_id}", status_code=303)


@router.post("/{cal_id}/powiadomienia", dependencies=[Depends(verify_csrf)])
def toggle_notify(cal_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    cal = _get(db, cal_id)
    cal.notify = not cal.notify
    db.commit()
    return RedirectResponse("/kalendarz/zewnetrzne", status_code=303)


@router.post("/{cal_id}/usun", dependencies=[Depends(verify_csrf)])
def remove(cal_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(_get(db, cal_id))
    db.commit()
    return RedirectResponse("/kalendarz/zewnetrzne", status_code=303)
