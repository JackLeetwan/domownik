"""Shared shopping list: /zakupy (full page, auto-refresh) + a card on the dashboard."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik import services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.money import parse_pln
from domownik.domain.shopping import SECTIONS, guess_section, parse_entry
from domownik.models import ShoppingItem, Transaction, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/zakupy")
LIST_TARGET = "shopping-list"


def open_items(db: Session) -> list[ShoppingItem]:
    return list(db.scalars(select(ShoppingItem).where(ShoppingItem.bought_on.is_(None)).order_by(ShoppingItem.id)))


def grouped(items: list[ShoppingItem]) -> list[tuple[str, str, list[ShoppingItem]]]:
    """[(section, label, items)] in walking order, only non-empty, items not in the cart yet."""
    out = []
    for key, label in SECTIONS.items():
        rows = [i for i in items if i.checked_at is None and (i.section if i.section in SECTIONS else "inne") == key]
        if rows:
            out.append((key, label, rows))
    return out


def suggestions(db: Session, limit: int = 300) -> list[str]:
    """Names bought or added before, most frequent first – for the input's autocomplete."""
    counts = Counter(n.strip() for n in db.scalars(select(ShoppingItem.name)) if n)
    return [n for n, _ in counts.most_common(limit)]


def _section_for(db: Session, name: str) -> str:
    last = db.scalar(
        select(ShoppingItem.section)
        .where(func.lower(ShoppingItem.name) == name.lower())
        .order_by(ShoppingItem.id.desc())
        .limit(1)
    )
    return last or guess_section(name)


def _context(db: Session, user: User) -> dict:
    items = open_items(db)
    return {
        "user": user,
        "groups": grouped(items),
        "cart": sorted((i for i in items if i.checked_at), key=lambda i: i.checked_at, reverse=True),
        "open_count": sum(1 for i in items if i.checked_at is None),
        "sections": SECTIONS,
        "suggestions": suggestions(db),
    }


def _respond(request: Request, db: Session, user: User):
    """HTMX call from /zakupy → fresh list fragment; anything else → back to where it came from."""
    if request.headers.get("hx-target") == LIST_TARGET:
        return render(request, "partials/shopping_list.html", _context(db, user))
    nxt = request.query_params.get("next") or "/zakupy"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/zakupy"
    resp = RedirectResponse(nxt, status_code=303)
    resp.headers["HX-Redirect"] = nxt
    return resp


def _get(db: Session, sid: int) -> ShoppingItem:
    s = db.get(ShoppingItem, sid)
    if s is None:
        raise HTTPException(404, "Nie ma takiej pozycji")
    return s


@router.get("")
def shopping_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "shopping.html", _context(db, user))


@router.get("/lista")
def shopping_list(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "partials/shopping_list.html", _context(db, user))


@router.post("", dependencies=[Depends(verify_csrf)])
async def add(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    # several at once: 'mleko, 2 chleb, masło' or one per line
    raw = str(form.get("text") or "").replace("\n", ",")
    for part in raw.split(","):
        name, qty = parse_entry(part)
        if not name:
            continue
        existing = db.scalar(
            select(ShoppingItem).where(ShoppingItem.bought_on.is_(None), func.lower(ShoppingItem.name) == name.lower())
        )
        if existing:  # already on the list – just refresh it
            existing.checked_at = None
            existing.qty = qty or existing.qty
            continue
        db.add(
            ShoppingItem(
                name=name,
                qty=qty,
                section=_section_for(db, name),
                note=(str(form.get("note") or "").strip() or None),
                item_id=int(form["item_id"]) if form.get("item_id") else None,
                added_by_id=user.id,
            )
        )
    db.commit()
    return _respond(request, db, user)


@router.post("/{sid}/check", dependencies=[Depends(verify_csrf)])
def check(request: Request, sid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = _get(db, sid)
    s.checked_at = None if s.checked_at else datetime.now()
    db.commit()
    return _respond(request, db, user)


@router.post("/{sid}/edit", dependencies=[Depends(verify_csrf)])
async def edit(request: Request, sid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = _get(db, sid)
    form = await request.form()
    s.name = (str(form.get("name") or "").strip() or s.name)[:120]
    s.qty = str(form.get("qty") or "").strip()[:30] or None
    s.note = str(form.get("note") or "").strip()[:200] or None
    if form.get("section") in SECTIONS:
        s.section = form["section"]
    db.commit()
    return _respond(request, db, user)


@router.post("/{sid}/delete", dependencies=[Depends(verify_csrf)])
def delete(request: Request, sid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(_get(db, sid))
    db.commit()
    return _respond(request, db, user)


@router.post("/zakoncz", dependencies=[Depends(verify_csrf)])
async def finish(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Everything in the cart is bought; optional receipt total goes to the budget (Jedzenie)."""
    form = await request.form()
    t = today()
    bought = [i for i in open_items(db) if i.checked_at]
    for i in bought:
        i.bought_on = t
    amount = parse_pln(form.get("amount"))
    if amount:
        cat = services.budget_category_by_name(db, "Jedzenie", "variable")
        shop = str(form.get("shop") or "").strip()
        db.add(
            Transaction(
                booked_on=t,
                amount_gr=-abs(amount),
                budget_category_id=cat.id if cat else None,
                note=f"Zakupy{' – ' + shop if shop else ''} ({len(bought)} poz.)",
                created_by_id=user.id,
            )
        )
    db.commit()
    resp = RedirectResponse("/zakupy", status_code=303)
    resp.headers["HX-Redirect"] = "/zakupy"
    return resp
