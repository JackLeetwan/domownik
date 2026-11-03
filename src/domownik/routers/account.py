"""Ustawienia: notification devices and switches; push subscription API; the one-tap "done"."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import ics, menu, notify, services, webpush
from domownik.config import get_settings, now, today
from domownik.db import get_db
from domownik.models import PushSubscription, Task, User
from domownik.security import current_user, verify_csrf
from domownik.web import HERE, render

router = APIRouter()


def device_label(ua: str) -> str:
    """'Android · Chrome' – enough to tell a user's devices apart."""
    ua_l = ua.lower()
    system = next(
        (
            name
            for key, name in (
                ("iphone", "iPhone"),
                ("ipad", "iPad"),
                ("android", "Android"),
                ("windows", "Windows"),
                ("mac os", "Mac"),
                ("linux", "Linux"),
            )
            if key in ua_l
        ),
        "Urządzenie",
    )
    browser = next(
        (
            name
            for key, name in (
                ("edg/", "Edge"),
                ("firefox", "Firefox"),
                ("samsungbrowser", "Samsung Internet"),
                ("chrome", "Chrome"),
                ("safari", "Safari"),
            )
            if key in ua_l
        ),
        "przeglądarka",
    )
    return f"{system} · {browser}"


def _devices(db: Session, user: User) -> list[PushSubscription]:
    stmt = select(PushSubscription).where(PushSubscription.user_id == user.id).order_by(PushSubscription.created_at)
    return list(db.scalars(stmt))


def _back(request: Request, *, message: str | None = None, error: str | None = None) -> RedirectResponse:
    """Post/redirect/get – a refresh must not repeat the action; the message survives in the session."""
    request.session["flash"] = {"message": message, "error": error}
    return RedirectResponse("/ustawienia", status_code=303)


@router.get("/ustawienia")
def settings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = get_settings()
    t = today()
    flash = request.session.pop("flash", None) or {}
    morning = notify.build_digests(db, "morning", t, preview_for=user)
    evening = notify.build_digests(db, "evening", t, preview_for=user)
    return render(
        request,
        "account.html",
        {
            "user": user,
            "devices": _devices(db, user),
            "morning_hour": s.morning_hour,
            "evening_hour": s.evening_hour,
            "morning": morning[0] if morning else None,
            "evening": evening[0] if evening else None,
            "message": flash.get("message"),
            "error": flash.get("error"),
            "any_assigned": services.any_assigned(db),
            "ics_url": flash.get("ics_url"),
            "ics_scopes": ics.SCOPES,
            "menu_choices": menu.BAR_CHOICES,
            "menu_links": menu.LINKS,
        },
    )


@router.post("/ustawienia/menu", dependencies=[Depends(verify_csrf)])
async def save_menu(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    menu.save(user, await request.form())
    db.commit()
    request.session["flash"] = {"message": "Zapisano menu."}
    return RedirectResponse("/ustawienia#menu", status_code=303)


@router.post("/ustawienia", dependencies=[Depends(verify_csrf)])
async def save_settings(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    user.notify_morning = bool(form.get("notify_morning"))
    user.notify_evening = bool(form.get("notify_evening"))
    db.commit()
    return _back(request, message="Zapisano.")


@router.post("/ustawienia/test", dependencies=[Depends(verify_csrf)])
def send_test(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not _devices(db, user):
        return _back(request, error="Najpierw włącz powiadomienia na telefonie.")
    digests = notify.build_digests(db, "morning", today(), preview_for=user)
    try:
        n = notify.send_test(db, user, digests[0] if digests else None)
    except webpush.PushError as exc:
        return _back(request, error=f"Nie udało się wysłać: {exc}")
    if not n:
        return _back(request, error="Urządzenie wyłączyło powiadomienia – włącz je ponownie.")
    return _back(request, message=f"Wysłano powiadomienie próbne ({n} {'urządzenie' if n == 1 else 'urządzenia'}).")


@router.post("/ustawienia/urzadzenia/{sub_id}/usun", dependencies=[Depends(verify_csrf)])
def remove_device(request: Request, sub_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    sub = db.get(PushSubscription, sub_id)
    if sub and sub.user_id == user.id:
        db.delete(sub)
        db.commit()
    return _back(request, message="Usunięto urządzenie.")


# ---------------------------------------------------------------- calendar subscription (ICS)


def _site_url(request: Request) -> str:
    """The address phones use: DOMOWNIK_BASE_URL when it is set, else the one this page was opened at."""
    base = get_settings().base_url.rstrip("/")
    if not base or base == "http://localhost:8000":
        base = str(request.base_url).rstrip("/")
    return base


@router.post("/ustawienia/kalendarz/nowy", dependencies=[Depends(verify_csrf)])
def ics_new_link(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    token = ics.new_token(user)
    db.commit()
    # shown once (only the hash is stored) – carried to the next page view in the session, then dropped
    request.session["flash"] = {"message": "Nowy link gotowy – poprzedni (jeśli był) już nie działa.", "error": None}
    request.session["flash"]["ics_url"] = f"{_site_url(request)}/ics/{token}.ics"
    return RedirectResponse("/ustawienia#kalendarz", status_code=303)


@router.post("/ustawienia/kalendarz/wylacz", dependencies=[Depends(verify_csrf)])
def ics_off(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    user.ics_token_hash = None
    db.commit()
    request.session["flash"] = {"message": "Link do kalendarza wyłączony.", "error": None}
    return RedirectResponse("/ustawienia#kalendarz", status_code=303)


@router.post("/ustawienia/kalendarz/zakres", dependencies=[Depends(verify_csrf)])
async def ics_scope(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    if form.get("scope") in ics.SCOPES:
        user.ics_scope = form["scope"]
        db.commit()
    request.session["flash"] = {
        "message": "Zapisano – kalendarz pobierze zmiany przy najbliższym odświeżeniu.",
        "error": None,
    }
    return RedirectResponse("/ustawienia#kalendarz", status_code=303)


@router.get("/ics/{token}.ics")
def ics_feed(token: str, db: Session = Depends(get_db)):
    """No session – calendar apps cannot log in; the personal token in the URL authorises."""
    user = ics.user_for_token(db, token)
    if user is None:
        return Response("Nie ma takiego kalendarza.", status_code=404, media_type="text/plain; charset=utf-8")
    body = ics.build(db, user, today(), now())
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="domownik.ics"',
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


# ---------------------------------------------------------------- push subscription API (static/app.js)


@router.get("/push/key")
def push_key(user: User = Depends(current_user)):
    return {"key": webpush.public_key()}


@router.post("/push/subscribe", dependencies=[Depends(verify_csrf)])
async def push_subscribe(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = await request.json()
    endpoint = str(data.get("endpoint") or "")
    keys = data.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "Nieprawidłowa subskrypcja")
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if sub is None:
        sub = PushSubscription(endpoint=endpoint)
        db.add(sub)
    # the same browser may change hands (logout → other person logs in): it follows the logged-in user
    sub.user_id = user.id
    sub.p256dh = keys["p256dh"]
    sub.auth = keys["auth"]
    sub.label = device_label(request.headers.get("user-agent", ""))
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/push/unsubscribe", dependencies=[Depends(verify_csrf)])
async def push_unsubscribe(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = await request.json()
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == str(data.get("endpoint") or "")))
    if sub and sub.user_id == user.id:
        db.delete(sub)
        db.commit()
    return JSONResponse({"ok": True})


@router.get("/sw.js")
def service_worker():
    # served from the root so its scope covers the whole app
    return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- one-tap "done"


@router.post("/n/done/{token}")
def done_from_push(token: str, db: Session = Depends(get_db)):
    """'✓' button in a notification. No session (the service worker calls it) – the signed token authorises."""
    data = notify.read_done_token(token)
    if data is None:
        return HTMLResponse("Link wygasł albo jest nieprawidłowy.", status_code=403)
    task = db.get(Task, data["t"])
    user = db.get(User, data["u"])
    if task is None or user is None or not user.is_active:
        return HTMLResponse("Nie ma takiego zadania.", status_code=404)
    if not task.active or task.next_due_date is None or task.next_due_date.isoformat() != data["d"]:
        return HTMLResponse(f"Już odhaczone: {task.full_title}")  # tapped twice, or done in the app meanwhile
    title = task.full_title  # before completing: the series number moves on
    services.complete_task(
        db, task, user=user, done_on=today(), amount_gr=task.amount_gr, km=None, vendor=None, notes="Z powiadomienia"
    )
    return HTMLResponse(f"Zrobione: {title}")


@router.get("/n/done/{token}")
def done_link_opened(token: str):
    # someone opened the action URL in a browser – do nothing on GET
    return RedirectResponse("/", status_code=303)
