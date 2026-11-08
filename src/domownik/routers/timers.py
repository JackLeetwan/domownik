"""⏱ Timers: started from "＋ Dodaj", shown on the dashboard, closed there or from the push."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from domownik import timers
from domownik.config import get_settings
from domownik.db import get_db
from domownik.models import Timer, User
from domownik.security import current_user, verify_csrf
from domownik.web import error_target, is_htmx, is_quick, quick_saved, render

router = APIRouter()


def card_context(db: Session) -> dict:
    tz = get_settings().tz
    rows = [{"timer": t, "ends_ms": int(t.ends_at.replace(tzinfo=tz).timestamp() * 1000)} for t in timers.active(db)]
    return {"timers": rows, "duration_text": timers.duration_text}


def _error(request: Request, message: str) -> HTMLResponse:
    return HTMLResponse(
        f'<p class="error">{escape(message)}</p>',
        status_code=422,
        headers={"HX-Retarget": error_target(request, "#timer-error"), "HX-Reswap": "innerHTML"},
    )


def _minutes(form) -> int | None:
    own = (form.get("own_minutes") or "").strip()
    raw = own or (form.get("minutes") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= timers.MAX_MINUTES else None


@router.post("/timery", dependencies=[Depends(verify_csrf)])
async def start(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    minutes = _minutes(form)
    if minutes is None:
        return _error(request, f"Podaj czas w minutach: od 1 do {timers.MAX_MINUTES}.")
    t = timers.start(db, user, str(form.get("title") or ""), minutes)
    if is_quick(request):
        return quick_saved(f"⏱ {t.title}: przypomnę o {t.ends_at:%H:%M}")
    return RedirectResponse("/", status_code=303)


def _timer(db: Session, timer_id: int) -> Timer:
    t = db.get(Timer, timer_id)
    if t is None:
        raise HTTPException(404, "Nie ma takiego timera")
    return t


def _card(request: Request, db: Session, user: User):
    if is_htmx(request):
        return render(request, "partials/timers.html", {"user": user} | card_context(db))
    return RedirectResponse("/", status_code=303)


@router.post("/timery/{timer_id}/plus", dependencies=[Depends(verify_csrf)])
def plus(timer_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = _timer(db, timer_id)
    if t.finished_at is None:
        timers.extend(db, t)
    return _card(request, db, user)


@router.post("/timery/{timer_id}/koniec", dependencies=[Depends(verify_csrf)])
def finish(timer_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    timers.finish(db, _timer(db, timer_id))
    return _card(request, db, user)


# ---------------------------------------------------------------- buttons in the push (no session)


@router.post("/n/timer/{token}/{action}")
def from_push(token: str, action: str, db: Session = Depends(get_db)):
    """'✓ Zrobione' / '+15 min' in the notification – the signed token authorises, like /n/done."""
    timer_id = timers.read_token(token)
    t = db.get(Timer, timer_id) if timer_id else None
    if t is None or action not in ("done", "plus"):
        return PlainTextResponse("Link wygasł albo jest nieprawidłowy.", status_code=403)
    if t.finished_at is not None:
        return PlainTextResponse(f"Już zakończone: {t.title}")
    if action == "done":
        timers.finish(db, t)
        return PlainTextResponse(f"Zrobione: {t.title}")
    timers.extend(db, t)
    return PlainTextResponse(f"{t.title}: przypomnę o {t.ends_at:%H:%M}")


@router.get("/n/timer/{token}/{action}")
def push_link_opened(token: str, action: str):
    return RedirectResponse("/", status_code=303)  # opened in a browser – do nothing on GET
