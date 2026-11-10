"""💳 /zakupy/karty – the house's loyalty cards, a barcode to show at the till (see domownik.loyalty)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import loyalty
from domownik.config import now
from domownik.db import get_db
from domownik.models import LoyaltyCard, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/zakupy/karty")


def _go(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _cards(db: Session) -> list[LoyaltyCard]:
    rows = list(db.scalars(select(LoyaltyCard)))
    # the ones shown at a till lately first, the rest by name
    return sorted(rows, key=lambda c: (c.used_at is None, -(c.used_at.timestamp() if c.used_at else 0), c.name.lower()))


def _card(db: Session, cid: int) -> LoyaltyCard:
    c = db.get(LoyaltyCard, cid)
    if c is None:
        raise HTTPException(404, "Nie ma takiej karty")
    return c


def _list_page(request: Request, db: Session, user: User, *, error: str | None = None, form=None, status=200):
    return render(
        request,
        "loyalty_cards.html",
        {"user": user, "cards": _cards(db), "error": error, "form": form or {}},
        status_code=status,
    )


@router.get("")
def cards_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _list_page(request, db, user)


def _name(form) -> str:
    name = str(form.get("name") or "").strip()[:60]
    if not name:
        raise loyalty.CardError("Wpisz nazwę sklepu, np. Rossmann")
    return name


@router.post("", dependencies=[Depends(verify_csrf)])
async def add_card(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    try:
        name = _name(form)
        code, fmt = loyalty.clean(str(form.get("code") or ""), str(form.get("fmt") or "") or None)
    except loyalty.CardError as exc:
        return _list_page(request, db, user, error=str(exc), form=form, status=400)
    c = LoyaltyCard(
        name=name, code=code, fmt=fmt, note=str(form.get("note") or "").strip()[:120] or None, created_by_id=user.id
    )
    db.add(c)
    db.commit()
    return _go(f"/zakupy/karty/{c.id}")


@router.get("/{cid:int}")
def show_card(request: Request, cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Big, on white, for the scanner at the till."""
    c = _card(db, cid)
    c.used_at = now()
    db.commit()
    return render(
        request,
        "loyalty_card.html",
        {"user": user, "c": c, "code_svg": loyalty.svg(c.code, c.fmt), "fmt_label": loyalty.FORMATS[c.fmt][0]},
    )


@router.post("/{cid:int}/edit", dependencies=[Depends(verify_csrf)])
async def edit_card(request: Request, cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _card(db, cid)
    form = await request.form()
    try:
        c.name = _name(form)
    except loyalty.CardError as exc:
        raise HTTPException(400, str(exc)) from None
    c.note = str(form.get("note") or "").strip()[:120] or None
    db.commit()
    return _go(f"/zakupy/karty/{c.id}")


@router.post("/{cid:int}/usun", dependencies=[Depends(verify_csrf)])
def delete_card(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(_card(db, cid))
    db.commit()
    return _go("/zakupy/karty")
