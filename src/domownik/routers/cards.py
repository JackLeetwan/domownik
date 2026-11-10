"""💳 /zakupy/karty – the house's loyalty cards, a barcode to show at the till (see domownik.loyalty)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import brands, documents, loyalty
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
    cards = _cards(db)
    return render(
        request,
        "loyalty_cards.html",
        {
            "user": user,
            "cards": cards,
            "logos": {c.id for c in cards if loyalty.logo_file(c)},
            "error": error,
            "form": form or {},
        },
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
    # the shop's logo and colour from its website (a few seconds; nothing lost if it fails)
    site = brands.host_of(str(form.get("site") or ""))
    loyalty.apply_brand(c, await run_in_threadpool(brands.look_up, name, site))
    db.commit()
    return _go(f"/zakupy/karty/{c.id}")


@router.get("/{cid:int}")
def show_card(request: Request, cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Big, on white, for the scanner at the till."""
    c = _card(db, cid)
    c.used_at = now()
    db.commit()
    return _show(request, db, user, c)


def _show(request: Request, db: Session, user: User, c: LoyaltyCard, *, error: str | None = None, status=200):
    return render(
        request,
        "loyalty_card.html",
        {
            "user": user,
            "c": c,
            "code_svg": loyalty.svg(c.code, c.fmt),
            "fmt_label": loyalty.FORMATS[c.fmt][0],
            "has_logo": loyalty.logo_file(c) is not None,
            "error": error,
        },
        status_code=status,
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
    color = str(form.get("color") or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", color):
        c.color = color
    site = brands.host_of(str(form.get("site") or ""))
    files = documents.uploads_in(form)
    try:
        if files:
            loyalty.store_logo(c, loyalty.own_logo(files[0].file))
        elif form.get("fetch") or (site and site != c.site):
            found = await run_in_threadpool(brands.look_up, c.name, site)
            if found is None:
                raise loyalty.CardError("Nie znalazłem strony tego sklepu – wpisz jej adres, np. rossmann.pl")
            loyalty.apply_brand(c, found)
            if found.logo is None and not files:
                db.commit()
                raise loyalty.CardError(
                    "Kolor jest, ale logo na stronie sklepu jest za małe – możesz wgrać własne (np. zdjęcie karty)"
                )
    except loyalty.CardError as exc:
        db.commit()
        return _show(request, db, user, c, error=str(exc), status=400)
    db.commit()
    return _go(f"/zakupy/karty/{c.id}")


@router.get("/{cid:int}/logo")
def card_logo(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    path = loyalty.logo_file(_card(db, cid))
    if path is None:
        raise HTTPException(404, "Ta karta nie ma logo")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=86400", "Content-Security-Policy": "sandbox"},
    )


@router.post("/{cid:int}/usun", dependencies=[Depends(verify_csrf)])
def delete_card(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _card(db, cid)
    loyalty.drop_logo(c)
    db.delete(c)
    db.commit()
    return _go("/zakupy/karty")
