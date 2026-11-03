"""/punkty: balances, confirmations, rewards, history, points given by hand."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import points, services
from domownik.db import get_db
from domownik.models import PointEntry, Reward, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/punkty")
HISTORY = 40


def _back() -> RedirectResponse:
    resp = RedirectResponse("/punkty", status_code=303)
    resp.headers["HX-Redirect"] = "/punkty"
    return resp


def _error(msg: str) -> HTMLResponse:
    return HTMLResponse(msg, status_code=400, headers={"HX-Reswap": "none"})


@router.get("")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    users = services.active_users(db)
    return render(
        request,
        "points.html",
        {
            "user": user,
            "users": users,
            "balances": points.balances(db, users),
            "mine": points.balance_of(db, user),
            "to_confirm": points.pending_for(db, user),
            "my_pending": list(
                db.scalars(select(PointEntry).where(PointEntry.user_id == user.id, PointEntry.status == "pending"))
            ),
            "rewards": list(db.scalars(select(Reward).where(Reward.active.is_(True)).order_by(Reward.cost))),
            "history": list(db.scalars(select(PointEntry).order_by(PointEntry.id.desc()).limit(HISTORY))),
        },
    )


def _entry(db: Session, eid: int) -> PointEntry:
    e = db.get(PointEntry, eid)
    if e is None:
        raise HTTPException(404, "Nie ma takiego wpisu")
    return e


@router.post("/{eid}/zatwierdz", dependencies=[Depends(verify_csrf)])
def approve(eid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        points.decide(_entry(db, eid), user, approve=True)
    except ValueError as exc:
        return _error(str(exc))
    db.commit()
    return _back()


@router.post("/{eid}/odrzuc", dependencies=[Depends(verify_csrf)])
def reject(eid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        points.decide(_entry(db, eid), user, approve=False)
    except ValueError as exc:
        return _error(str(exc))
    db.commit()
    return _back()


@router.post("/przyznaj", dependencies=[Depends(verify_csrf)])
async def give(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Points by hand ("pomógł przy zakupach") – for someone else, counted at once."""
    form = await request.form()
    try:
        who = db.get(User, int(form.get("user_id") or 0))
        n = int(str(form.get("points") or "").strip())
    except ValueError:
        return _error("Podaj liczbę punktów")
    if who is None or not who.is_active:
        return _error("Wybierz osobę")
    if who.id == user.id:
        return _error("Punkty dla siebie przyznaje ktoś inny z domowników")
    if n == 0 or abs(n) > points.MAX_POINTS:
        return _error(f"Punkty od 1 do {points.MAX_POINTS} (minus – żeby odjąć)")
    reason = " ".join(str(form.get("reason") or "").split())[:200] or ("Premia" if n > 0 else "Kara")
    db.add(
        PointEntry(
            user_id=who.id, points=n, reason=reason, status="approved", created_by_id=user.id, decided_by_id=user.id
        )
    )
    db.commit()
    return _back()


# ---------------------------------------------------------------- rewards


@router.post("/nagrody", dependencies=[Depends(verify_csrf)])
async def add_reward(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    name = " ".join(str(form.get("name") or "").split())[:120]
    try:
        cost = points.parse_points(form.get("cost"))
    except ValueError as exc:
        return _error(str(exc))
    if not name or not cost:
        return _error("Podaj nazwę i koszt nagrody w punktach")
    db.add(Reward(name=name, icon=str(form.get("icon") or "").strip()[:16] or None, cost=cost))
    db.commit()
    return _back()


def _reward(db: Session, rid: int) -> Reward:
    r = db.get(Reward, rid)
    if r is None:
        raise HTTPException(404, "Nie ma takiej nagrody")
    return r


@router.post("/nagrody/{rid}/odbierz", dependencies=[Depends(verify_csrf)])
def take_reward(rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        points.redeem(db, _reward(db, rid), user)
    except ValueError as exc:
        return _error(str(exc))
    db.commit()
    return _back()


@router.post("/nagrody/{rid}/usun", dependencies=[Depends(verify_csrf)])
def remove_reward(rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Hidden, not deleted – the history keeps pointing at it."""
    _reward(db, rid).active = False
    db.commit()
    return _back()
