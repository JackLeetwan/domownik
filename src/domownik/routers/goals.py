"""Savings goals in /budzet (section #cele): create, put money in / take it out, close, delete."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from domownik import goals
from domownik.config import today
from domownik.db import get_db
from domownik.domain.money import parse_pln
from domownik.models import SavingsGoal, User
from domownik.security import current_user, verify_csrf
from domownik.web import is_quick, quick_saved

router = APIRouter(prefix="/budzet/cele")


def _back(form) -> RedirectResponse:
    m = str(form.get("m") or "")
    url = "/budzet" + (f"?m={m}" if len(m) == 7 and m[4] == "-" else "") + "#cele"
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _error(msg: str) -> HTMLResponse:
    # shown as a toast (400 → just the message, see app.js); the form stays as it was
    return HTMLResponse(msg, status_code=400, headers={"HX-Reswap": "none"})


def _goal(db: Session, goal_id: int) -> SavingsGoal:
    g = db.get(SavingsGoal, goal_id)
    if g is None:
        raise HTTPException(404, "Nie ma takiego celu")
    return g


def _date(raw) -> date | None:
    raw = str(raw or "").strip()
    return date.fromisoformat(raw) if raw else None


def _fill(g: SavingsGoal, form) -> None:
    name = str(form.get("name") or "").strip()[:120]
    target = parse_pln(form.get("target"))
    if not name:
        raise ValueError("Nazwij cel, np. „Wakacje 2027”")
    if not target or target <= 0:
        raise ValueError("Podaj kwotę celu")
    g.name, g.target_gr = name, target
    g.icon = str(form.get("icon") or "").strip()[:16] or None
    g.due_on = _date(form.get("due_on"))


@router.post("", dependencies=[Depends(verify_csrf)])
async def create(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    g = SavingsGoal(created_by_id=user.id)
    try:
        _fill(g, form)
        db.add(g)
        db.flush()
        start = parse_pln(form.get("start"))
        if start:
            goals.move(db, g, start, user=user, on=today())
    except ValueError as exc:
        db.rollback()
        return _error(str(exc))
    db.commit()
    if is_quick(request):
        return quick_saved("Dodano cel")
    return _back(form)


@router.post("/{goal_id}/wplata", dependencies=[Depends(verify_csrf)])
async def deposit(request: Request, goal_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    g = _goal(db, goal_id)
    try:
        goals.move(db, g, parse_pln(form.get("amount")) or 0, user=user, on=today(), withdraw=form.get("kind") == "out")
    except ValueError as exc:
        db.rollback()
        return _error(str(exc))
    db.commit()
    return _back(form)


@router.post("/{goal_id}/edycja", dependencies=[Depends(verify_csrf)])
async def edit(request: Request, goal_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    g = _goal(db, goal_id)
    try:
        _fill(g, form)
    except ValueError as exc:
        db.rollback()
        return _error(str(exc))
    db.commit()
    return _back(form)


@router.post("/{goal_id}/zakoncz", dependencies=[Depends(verify_csrf)])
async def close(request: Request, goal_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Reached / given up → "Zrealizowane"; again → back among the active ones."""
    form = await request.form()
    g = _goal(db, goal_id)
    g.done_on = None if g.done_on else today()
    db.commit()
    return _back(form)


@router.post("/{goal_id}/usun", dependencies=[Depends(verify_csrf)])
async def delete(request: Request, goal_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The deposits stay in the budget (as "Oszczędności") – only the goal goes."""
    form = await request.form()
    db.delete(_goal(db, goal_id))
    db.commit()
    return _back(form)
