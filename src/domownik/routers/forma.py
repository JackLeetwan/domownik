"""💪 Forma – personal health habits; for now the supplements. Everything here is the logged-in person's own."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from domownik import supplements as supl
from domownik.config import now, today
from domownik.db import get_db
from domownik.domain.money import parse_pln
from domownik.models import Supplement, User
from domownik.security import current_user, verify_csrf
from domownik.web import error_target, is_htmx, render

router = APIRouter()


def card_context(db: Session, user: User) -> dict:
    t = today()
    hour = now().strftime("%H:%M")
    groups = supl.today_groups(db, user, t)
    return {
        "supl_groups": groups,
        "supl_open": sum(len(g.open) for g in groups),
        # the dashboard shows the card only from the hour of something still open (20:00 creatine not all day)
        "supl_due": any(g.open and (g.time is None or g.time <= hour) for g in groups),
        "supl_streak": supl.streak(db, user, t) if groups else 0,
    }


def _error(request: Request, message: str) -> HTMLResponse:
    return HTMLResponse(
        f'<p class="error">{escape(message)}</p>',
        status_code=422,
        headers={"HX-Retarget": error_target(request, "#supl-error"), "HX-Reswap": "innerHTML"},
    )


def _back(anchor: str = "") -> RedirectResponse:
    url = "/forma" + anchor
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _own(db: Session, user: User, supplement_id: int) -> Supplement:
    s = supl.get_own(db, user, supplement_id)
    if s is None:  # someone else's is the same as none – not even its existence leaks
        raise HTTPException(404, "Nie ma takiego suplementu")
    return s


@router.get("/forma")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    active = supl.mine(db, user)
    names = {s.name.lower() for s in supl.mine(db, user, active=None)}
    return render(
        request,
        "forma.html",
        {
            "user": user,
            "supplements": active,
            "paused": supl.mine(db, user, active=False),
            "suggestions": [(n, d) for n, d in supl.SUGGESTIONS if n.lower() not in names],
            "days_left": supl.days_left,
            "fmt": supl.fmt,
            "card_where": "forma",
        }
        | card_context(db, user),
    )


def _fields(form) -> dict:
    name = str(form.get("name") or "").strip()
    if not name:
        raise ValueError("Podaj nazwę suplementu")
    return {
        "name": name,
        "dose": str(form.get("dose") or "").strip() or None,
        "time": supl.parse_time(form.get("time")),
        "per_take": supl.parse_number(form.get("per_take"), "Na raz") or 1,
        "pack_size": supl.parse_number(form.get("pack_size"), "Opakowanie"),
        "stock": supl.parse_number(form.get("stock"), "Zostało", allow_zero=True),
    }


@router.post("/forma/suplementy", dependencies=[Depends(verify_csrf)])
async def add(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    try:
        f = _fields(form)
    except ValueError as exc:
        return _error(request, str(exc))
    if f["stock"] is None:
        f["stock"] = f["pack_size"]  # a new pack just opened
    s = supl.create(db, user, **f)
    db.commit()
    return _back(f"#s{s.id}")


@router.post("/forma/suplementy/{supplement_id}", dependencies=[Depends(verify_csrf)])
async def edit(supplement_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = _own(db, user, supplement_id)
    form = await request.form()
    try:
        f = _fields(form)
        restock_days = int(str(form.get("restock_days") or s.restock_days))
        if not 0 <= restock_days <= 90:
            raise ValueError("Ostrzeżenie o zapasie: od 0 do 90 dni")
    except ValueError as exc:
        return _error(request, str(exc))
    s.name, s.dose, s.per_take, s.pack_size, s.stock = f["name"], f["dose"], f["per_take"], f["pack_size"], f["stock"]
    s.restock_days = restock_days
    supl.set_time(s, f["time"])
    left = supl.days_left(s)
    if s.restock_listed and (left is None or left > s.restock_days):
        s.restock_listed = False  # stock corrected by hand – the next run-out is noticed again
    supl.check_restock(db, s)
    db.commit()
    return _back(f"#s{s.id}")


def _card(request: Request, db: Session, user: User, where: str):
    if is_htmx(request):
        return render(
            request, "partials/supplements_today.html", {"user": user, "card_where": where} | card_context(db, user)
        )
    return RedirectResponse("/" if where == "pulpit" else "/forma", status_code=303)


@router.post("/forma/suplementy/{supplement_id}/wziete", dependencies=[Depends(verify_csrf)])
async def toggle(
    supplement_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    s = _own(db, user, supplement_id)
    form = await request.form()
    t = today()
    if not supl.untake(db, s, t):
        supl.take(db, s, t)
    db.commit()
    return _card(request, db, user, str(form.get("where") or "forma"))


@router.post("/forma/suplementy/wziete-wszystkie", dependencies=[Depends(verify_csrf)])
async def take_group(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """'✓ Wszystkie' of one hour on the card."""
    form = await request.form()
    time = str(form.get("time") or "") or None
    t = today()
    for s in supl.mine(db, user):
        if s.time == time:
            supl.take(db, s, t)
    db.commit()
    return _card(request, db, user, str(form.get("where") or "forma"))


@router.post("/forma/suplementy/{supplement_id}/kupione", dependencies=[Depends(verify_csrf)])
async def bought(
    supplement_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    s = _own(db, user, supplement_id)
    form = await request.form()
    try:
        packs = int(str(form.get("packs") or "1"))
        if not 1 <= packs <= 20:
            raise ValueError("Liczba opakowań: od 1 do 20")
    except ValueError as exc:
        return _error(request, str(exc))
    supl.bought_pack(db, s, user, price_gr=parse_pln(form.get("price")), day=today(), packs=packs)
    db.commit()
    return _back(f"#s{s.id}")


@router.post("/forma/suplementy/{supplement_id}/wstrzymaj", dependencies=[Depends(verify_csrf)])
def pause(supplement_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    supl.pause(_own(db, user, supplement_id))
    db.commit()
    return _back()


@router.post("/forma/suplementy/{supplement_id}/wznow", dependencies=[Depends(verify_csrf)])
def resume(supplement_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = _own(db, user, supplement_id)
    supl.resume(s)
    db.commit()
    return _back(f"#s{s.id}")


@router.post("/forma/suplementy/{supplement_id}/usun", dependencies=[Depends(verify_csrf)])
def remove(supplement_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(_own(db, user, supplement_id))
    db.commit()
    return _back()


# ---------------------------------------------------------------- the button in the push (no session)


@router.post("/n/supl/{token}")
def from_push(token: str, db: Session = Depends(get_db)):
    """'✓ Wzięte' in the notification – the signed token names the person, the day and the supplements."""
    data = supl.read_token(token)
    user = db.get(User, data[0]) if data else None
    if data is None or user is None:
        return PlainTextResponse("Link wygasł albo jest nieprawidłowy.", status_code=403)
    _, day, ids = data
    names = []
    for sid in ids:
        s = supl.get_own(db, user, sid)
        if s is not None:
            supl.take(db, s, day)
            names.append(s.name)
    db.commit()
    if not names:
        return PlainTextResponse("Nie ma już tych suplementów.")
    n = supl.streak(db, user, today())
    tail = f" · seria {n} {'dzień' if n == 1 else 'dni'}" if n else ""
    return PlainTextResponse(f"Wzięte: {', '.join(names)}{tail}")


@router.get("/n/supl/{token}")
def push_link_opened(token: str):
    return RedirectResponse("/forma", status_code=303)  # opened in a browser – do nothing on GET
