"""/kontakty – who to call: tradespeople, car service, kindergarten, insurance agent, doctors.

A contact can be linked to several items (one mechanic for both cars); the item page shows its own.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domownik.db import get_db
from domownik.domain.text import fold, terms
from domownik.domain.tree import Tree
from domownik.models import CONTACT_ROLES, Contact, Item, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter()

OTHER_ROLE = "Inne"
PREVIEW = 3  # contacts shown on an item page before "wszystkie"


def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _safe_next(raw, fallback: str) -> str:
    nxt = str(raw or "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else fallback


def tel_href(phone: str) -> str:
    """'+48 600 100 200' → 'tel:+48600100200' (digits, a leading + and the */# of service codes)."""
    return "tel:" + re.sub(r"[^\d+*#]", "", phone)


def map_href(address: str) -> str:
    """A maps search for the address – nothing is sent anywhere until the link is tapped."""
    return "https://www.google.com/maps/search/?api=1&query=" + quote(address)


def clean_website(raw: str | None) -> str | None:
    """'example.pl' → 'https://example.pl'; anything that is not a plain web address is dropped."""
    value = (raw or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    return value[:300] if re.match(r"^https?://[^\s/$.?#][^\s]*$", value, re.I) else None


def matches(c: Contact, words: list[str]) -> bool:
    blob = fold(
        "\n".join(
            [c.name, c.company or "", c.role or "", c.phones or "", c.email or "", c.address or "", c.notes or ""]
            + [i.name for i in c.items]
        )
    )
    return all(w in blob for w in words)


def grouped(contacts: list[Contact]) -> list[tuple[str, str, list[Contact]]]:
    """[(role, icon, contacts)]: proposed roles in their order, then own roles A–Z, 'Inne' last."""
    by_role: dict[str, list[Contact]] = {}
    for c in contacts:
        by_role.setdefault((c.role or "").strip() or OTHER_ROLE, []).append(c)
    order = [r for r in CONTACT_ROLES if r in by_role]
    order += sorted((r for r in by_role if r not in CONTACT_ROLES and r != OTHER_ROLE), key=fold)
    if OTHER_ROLE in by_role:
        order.append(OTHER_ROLE)
    return [(r, CONTACT_ROLES.get(r, "📇"), sorted(by_role[r], key=lambda c: fold(c.name))) for r in order]


def all_contacts(db: Session) -> list[Contact]:
    return list(db.scalars(select(Contact).options(selectinload(Contact.items)).order_by(Contact.name)))


def vendor_suggestions(db: Session, item_id: int | None = None) -> list[str]:
    """Names for the "Wykonawca" field: contacts of this item first, then everybody else."""
    contacts = all_contacts(db)
    own = [c for c in contacts if any(i.id == item_id for i in c.items)]
    rest = [c for c in contacts if c not in own]
    return [c.label for c in own + rest]


def _get(db: Session, cid: int) -> Contact:
    c = db.get(Contact, cid)
    if c is None:
        raise HTTPException(404, "Nie ma takiego kontaktu")
    return c


def _item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Nie ma takiego elementu")
    return item


def _apply(db: Session, c: Contact, form) -> None:
    name = str(form.get("name") or "").strip()[:120]
    company = str(form.get("company") or "").strip()[:120] or None
    if not name and not company:
        raise HTTPException(400, "Podaj nazwę albo firmę")
    c.name = name or company or ""
    c.company = company
    c.role = str(form.get("role") or "").strip()[:60] or None
    phones = [p.strip() for p in re.split(r"[\n;]", str(form.get("phones") or "")) if p.strip()]
    c.phones = "\n".join(phones) or None
    c.email = str(form.get("email") or "").strip()[:200] or None
    c.website = clean_website(form.get("website"))
    c.address = str(form.get("address") or "").strip()[:300] or None
    c.notes = str(form.get("notes") or "").strip() or None
    ids = {int(v) for v in form.getlist("item_id") if str(v).isdigit()}
    c.items = list(db.scalars(select(Item).where(Item.id.in_(ids)))) if ids else []


def _form_ctx(db: Session, user: User, contact: Contact | None, linked: set[int], nxt: str) -> dict:
    tree = Tree.load(db)
    return {
        "user": user,
        "contact": contact,
        "linked": linked,
        "items": sorted(tree.items.values(), key=lambda i: tree.label(i.id)),
        "label": tree.label,
        "roles": CONTACT_ROLES,
        "next": nxt,
    }


@router.get("/kontakty")
def contacts_page(
    request: Request,
    q: str = "",
    item: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    contacts = all_contacts(db)
    total = len(contacts)
    the_item = _item(db, item) if item else None
    if the_item is not None:
        contacts = [c for c in contacts if any(i.id == the_item.id for i in c.items)]
    words = terms(q)
    if words:
        contacts = [c for c in contacts if matches(c, words)]
    return render(
        request,
        "contacts.html",
        {
            "user": user,
            "groups": grouped(contacts),
            "count": len(contacts),
            "total": total,
            "q": q,
            "the_item": the_item,
            "tel_href": tel_href,
            "map_href": map_href,
        },
    )


@router.get("/kontakty/nowy")
def new_contact(
    request: Request, item: int | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    linked = {item} if item else set()
    nxt = f"/items/{item}#kontakty" if item else "/kontakty"
    return render(request, "contact_form.html", _form_ctx(db, user, None, linked, nxt))


@router.post("/kontakty/nowy", dependencies=[Depends(verify_csrf)])
async def create_contact(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    c = Contact(created_by_id=user.id)
    _apply(db, c, form)
    db.add(c)
    db.commit()
    return _redirect(_safe_next(form.get("next"), f"/kontakty#c{c.id}"))


@router.get("/kontakty/{cid}/edit")
def edit_contact(
    request: Request, cid: int, next: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)
):
    c = _get(db, cid)
    return render(
        request, "contact_form.html", _form_ctx(db, user, c, {i.id for i in c.items}, _safe_next(next, "/kontakty"))
    )


@router.post("/kontakty/{cid}/edit", dependencies=[Depends(verify_csrf)])
async def update_contact(request: Request, cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _get(db, cid)
    form = await request.form()
    _apply(db, c, form)
    db.commit()
    return _redirect(_safe_next(form.get("next"), f"/kontakty#c{c.id}"))


@router.post("/kontakty/{cid}/delete", dependencies=[Depends(verify_csrf)])
def delete_contact(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(_get(db, cid))
    db.commit()
    return _redirect("/kontakty")


@router.post("/items/{item_id}/kontakty", dependencies=[Depends(verify_csrf)])
async def link_contact(
    request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    item = _item(db, item_id)
    form = await request.form()
    raw = str(form.get("contact_id") or "")
    if not raw.isdigit():
        raise HTTPException(400, "Wybierz kontakt")
    c = _get(db, int(raw))
    if item not in c.items:
        c.items.append(item)
    db.commit()
    return _redirect(f"/items/{item.id}#kontakty")


@router.post("/items/{item_id}/kontakty/{cid}/odepnij", dependencies=[Depends(verify_csrf)])
def unlink_contact(item_id: int, cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _item(db, item_id)
    c = _get(db, cid)
    if item in c.items:
        c.items.remove(item)
    db.commit()
    return _redirect(f"/items/{item.id}#kontakty")
