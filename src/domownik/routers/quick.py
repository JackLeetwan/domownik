"""Quick add ("＋" on every page): one window with short forms that post to the ordinary routes.

Nothing is saved here – each form reuses the route of its own page (/zakupy, /notatki, /do-zrobienia,
/budzet/wpis, /tasks/new …) with the X-Quick-Add header, so validation and side effects stay in one place.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik import budget, services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import BUDGET_KINDS, Meter, User
from domownik.routers import shopping
from domownik.routers.budget import _default_expense_category
from domownik.security import current_user
from domownik.web import render

router = APIRouter(prefix="/dodaj")

# kind: (icon, label, hint)
KINDS: dict[str, tuple[str, str, str]] = {
    "task": ("📋", "Zadanie", "termin, przypomnienie"),
    "shopping": ("🛒", "Zakup", "na listę zakupów"),
    "note": ("📝", "Notatka", "karteczka dla wszystkich"),
    "todo": ("☐", "Do zrobienia", "bez terminu"),
    "expense": ("💸", "Wydatek", "do budżetu"),
    "reading": ("🔢", "Odczyt licznika", "prąd, woda, gaz"),
}

ITEM_PAGE = re.compile(r"^/items/(\d+)(?:/|$)")
METER_PAGE = re.compile(r"^/liczniki/(\d+)(?:/|$)")


def page_item_id(request: Request, tree: Tree) -> int | None:
    """The item whose page the window was opened on (from htmx's HX-Current-URL), if any."""
    path = urlsplit(request.headers.get("hx-current-url") or "").path
    m = ITEM_PAGE.match(path)
    if m and int(m.group(1)) in tree.items:
        return int(m.group(1))
    return None


def page_meter_id(request: Request) -> int | None:
    m = METER_PAGE.match(urlsplit(request.headers.get("hx-current-url") or "").path)
    return int(m.group(1)) if m else None


def meter_choices(db: Session, tree: Tree) -> list[Meter]:
    """Meters of items that are not archived, in tree order."""
    rows = [m for m in db.scalars(select(Meter)) if m.item_id in tree.items]
    return sorted(rows, key=lambda m: (tree.label(m.item_id), m.sort_order, m.id))


@router.get("")
def menu(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    kinds = dict(KINDS)
    if db.scalar(select(func.count(Meter.id))) == 0:
        kinds.pop("reading")  # nothing to read yet – meters are added on the item page
    return render(request, "partials/quick_add.html", {"user": user, "kind": None, "kinds": kinds})


@router.get("/{kind}")
def form(request: Request, kind: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if kind not in KINDS:
        raise HTTPException(404, "Nie ma takiego formularza")
    tree = Tree.load(db)
    ctx: dict = {
        "user": user,
        "kind": kind,
        "kinds": KINDS,
        "items": sorted(tree.items.values(), key=lambda i: tree.label(i.id)),
        "label": tree.label,
        "current_item": page_item_id(request, tree),
        "users": services.active_users(db),
        "today": today(),
    }
    if kind == "shopping":
        ctx["suggestions"] = shopping.suggestions(db)
    elif kind == "expense":
        cats = budget.active_categories(db)
        ctx["categories"] = cats
        ctx["budget_kinds"] = BUDGET_KINDS
        ctx["default_cat"] = budget.last_used_category(db, user.id) or _default_expense_category(cats)
    elif kind == "reading":
        choices = meter_choices(db, tree)
        current = page_meter_id(request)
        if current is None:
            current = next((m.id for m in choices if m.item_id == ctx["current_item"]), None)
        ctx["meters"] = choices
        ctx["current_meter"] = current
    return render(request, "partials/quick_add.html", ctx)
