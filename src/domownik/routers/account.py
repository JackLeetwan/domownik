"""Ustawienia: the user's own notification settings, and the one-tap "done" from a push."""

from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from domownik import notify, services
from domownik.config import get_settings, today
from domownik.db import get_db
from domownik.models import Task, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter()

TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")


def _page(request: Request, db: Session, user: User, *, message: str | None = None, error: str | None = None):
    s = get_settings()
    t = today()
    morning = notify.build_digests(db, "morning", t, preview_for=user)
    evening = notify.build_digests(db, "evening", t, preview_for=user)
    return render(
        request,
        "account.html",
        {
            "user": user,
            "server": s.ntfy_server.rstrip("/"),
            "morning_hour": s.morning_hour,
            "evening_hour": s.evening_hour,
            "morning": morning[0] if morning else None,
            "evening": evening[0] if evening else None,
            "message": message,
            "error": error,
        },
        status_code=422 if error else 200,
    )


def _back(request: Request, *, message: str | None = None, error: str | None = None) -> RedirectResponse:
    """Post/redirect/get – a refresh must not repeat the action; the message survives in the session."""
    request.session["flash"] = {"message": message, "error": error}
    return RedirectResponse("/ustawienia", status_code=303)


@router.get("/ustawienia")
def settings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    flash = request.session.pop("flash", None) or {}
    return _page(request, db, user, message=flash.get("message"), error=flash.get("error"))


@router.post("/ustawienia", dependencies=[Depends(verify_csrf)])
async def save_settings(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    topic = (form.get("ntfy_topic") or "").strip()
    if topic and not TOPIC_RE.match(topic):
        return _page(
            request, db, user, error="Temat: 12–64 znaki, tylko litery, cyfry, - i _. Najlepiej wygeneruj losowy."
        )
    user.ntfy_topic = topic or None
    user.notify_morning = bool(form.get("notify_morning"))
    user.notify_evening = bool(form.get("notify_evening"))
    db.commit()
    return _back(request, message="Zapisano.")


@router.post("/ustawienia/temat", dependencies=[Depends(verify_csrf)])
def generate_topic(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    user.ntfy_topic = notify.new_topic()
    db.commit()
    return _back(request, message="Wygenerowano nowy temat – zasubskrybuj go w aplikacji ntfy (krok 3).")


@router.post("/ustawienia/test", dependencies=[Depends(verify_csrf)])
def send_test(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not user.ntfy_topic:
        return _back(request, error="Najpierw ustaw temat ntfy.")
    digests = notify.build_digests(db, "morning", today(), preview_for=user)
    try:
        notify.send_test(user, digests[0] if digests else None)
    except httpx.HTTPError as exc:
        return _back(request, error=f"Nie udało się wysłać: {exc}")
    return _back(request, message="Wysłano powiadomienie próbne – sprawdź telefon.")


@router.post("/n/done/{token}")
def done_from_push(token: str, db: Session = Depends(get_db)):
    """'✓' button in a push. No session (the ntfy app calls it) – the signed token is the authorisation."""
    data = notify.read_done_token(token)
    if data is None:
        return HTMLResponse("Link wygasł albo jest nieprawidłowy.", status_code=403)
    task = db.get(Task, data["t"])
    user = db.get(User, data["u"])
    if task is None or user is None or not user.is_active:
        return HTMLResponse("Nie ma takiego zadania.", status_code=404)
    if not task.active or task.next_due_date is None or task.next_due_date.isoformat() != data["d"]:
        return HTMLResponse("Już odhaczone.")  # tapped twice, or done in the app meanwhile
    services.complete_task(
        db, task, user=user, done_on=today(), amount_gr=task.amount_gr, km=None, vendor=None, notes="Z powiadomienia"
    )
    return HTMLResponse(f"Zrobione: {task.title}")


@router.get("/n/done/{token}")
def done_link_opened(token: str):
    # someone opened the action URL in a browser – do nothing on GET
    return RedirectResponse("/", status_code=303)
