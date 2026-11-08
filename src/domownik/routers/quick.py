"""Quick add ("＋" on every page): one window with short forms that post to the ordinary routes.

Nothing is saved here – each form reuses the route of its own page (/zakupy, /notatki, /do-zrobienia,
/budzet/wpis, /tasks/new …) with the X-Quick-Add header, so validation and side effects stay in one place.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik import budget, services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import BUDGET_KINDS, Item, Meter, User
from domownik.routers import shopping
from domownik.routers.budget import _default_expense_category
from domownik.security import current_user
from domownik.web import render

router = APIRouter(prefix="/dodaj")

# kind: (icon, label, hint)
KINDS: dict[str, tuple[str, str, str]] = {
    "event": ("📅", "Wydarzenie", "wizyta, spotkanie – z godziną"),
    "task": ("📋", "Zadanie", "termin, przypomnienie"),
    "shopping": ("🛒", "Zakup", "na listę zakupów"),
    "note": ("📝", "Notatka", "karteczka dla wszystkich"),
    "todo": ("☐", "Do zrobienia", "bez terminu"),
    "expense": ("💸", "Wydatek", "do budżetu"),
    "reading": ("🔢", "Odczyt licznika", "prąd, woda, gaz"),
    "timer": ("⏱", "Timer", "pranie, piekarnik – przypomni po czasie"),
}

# opened for one calendar day (double click on a day): only what has a date of its own
DAY_KINDS = ("event", "task", "expense")

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


def _day(d: str | None) -> date | None:
    """?d=YYYY-MM-DD – the calendar day the window was opened for; anything else is ignored."""
    try:
        return date.fromisoformat(d) if d else None
    except ValueError:
        return None


def _day_ctx(day: date | None) -> dict:
    from domownik.routers.calendar import day_title

    return {"day": day, "day_q": f"?d={day.isoformat()}" if day else "", "day_title": day_title(day) if day else ""}


@router.get("")
def menu(request: Request, d: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    day = _day(d)
    if day:
        kinds = {k: KINDS[k] for k in DAY_KINDS}
    else:
        kinds = dict(KINDS)
        if db.scalar(select(func.count(Meter.id))) == 0:
            kinds.pop("reading")  # nothing to read yet – meters are added on the item page
    return render(request, "partials/quick_add.html", {"user": user, "kind": None, "kinds": kinds} | _day_ctx(day))


@router.get("/{kind}")
def form(
    request: Request,
    kind: str,
    d: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if kind not in KINDS:
        raise HTTPException(404, "Nie ma takiego formularza")
    day = _day(d)
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
        "start": day or today(),
    } | _day_ctx(day)
    if kind in ("event", "task"):
        from domownik.routers.tasks import DEFAULT_REMIND, REMIND_CHOICES

        ctx["remind_choices"] = REMIND_CHOICES
        ctx["default_remind"] = DEFAULT_REMIND
    if kind == "event":
        if ctx["current_item"] is None:
            # an appointment is usually someone's: start with the person's own page in the tree
            own = db.scalar(select(Item.id).where(Item.user_id == user.id, Item.archived.is_(False)))
            ctx["current_item"] = own if own in tree.items else None
    elif kind == "shopping":
        ctx["suggestions"] = shopping.suggestions(db)
    elif kind == "expense":
        cats = budget.active_categories(db)
        ctx["categories"] = cats
        ctx["budget_kinds"] = BUDGET_KINDS
        ctx["default_cat"] = budget.last_used_category(db, user.id) or _default_expense_category(cats)
    elif kind == "timer":
        from domownik import timers

        ctx["presets"] = timers.PRESETS
        from domownik.models import Chore

        titles = timers.recent_titles(db)
        titles += [c for c in db.scalars(select(Chore.name).where(Chore.uses_timer.is_(True))) if c not in titles]
        ctx["titles"] = titles or [timers.DEFAULT_TITLE]
        ctx["max_minutes"] = timers.MAX_MINUTES
        ctx["duration_text"] = timers.duration_text
    elif kind == "reading":
        choices = meter_choices(db, tree)
        current = page_meter_id(request)
        if current is None:
            current = next((m.id for m in choices if m.item_id == ctx["current_item"]), None)
        ctx["meters"] = choices
        ctx["current_meter"] = current
    return render(request, "partials/quick_add.html", ctx)
