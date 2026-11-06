"""Kitchen: week meal plan, recipes and the pantry – and the bridges to the shopping list.

Ingredients are plain lines ("200 g mąki"); the same parser as the shopping list turns them into
entries, and the pantry is matched by name so things already at home start unticked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domownik.domain.grocery_names import key as name_key
from domownik.domain.shopping import add_qty, parse_entry
from domownik.models import MEALS, MealPlan, PantryItem, Recipe

SOON_DAYS = 3  # pantry: "kończy się" this many days before the date
NOTIFY_DAYS = 1  # morning push: today / tomorrow …
NOTIFY_EXPIRED_DAYS = 7  # … and things that went off in the last week (not forever)
# shopping section → where it usually goes at home ("Kupione → do spiżarni")
PLACE_BY_SECTION = {"nabial": "fridge", "mieso": "fridge", "mrozonki": "freezer"}
_WORD = re.compile(r"\w+", re.UNICODE)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass
class PlanDay:
    day: date
    meals: dict[str, list[MealPlan]]

    @property
    def empty(self) -> bool:
        return not any(self.meals.values())


def week_plan(db: Session, monday: date) -> list[PlanDay]:
    rows = db.scalars(
        select(MealPlan)
        .where(MealPlan.day >= monday, MealPlan.day < monday + timedelta(days=7))
        .options(selectinload(MealPlan.recipe))
        .order_by(MealPlan.day, MealPlan.id)
    )
    days = [PlanDay(monday + timedelta(days=i), {k: [] for k in MEALS}) for i in range(7)]
    for r in rows:
        days[(r.day - monday).days].meals.setdefault(r.meal if r.meal in MEALS else "other", []).append(r)
    return days


def meals_on(db: Session, day: date) -> list[MealPlan]:
    order = list(MEALS)
    rows = db.scalars(select(MealPlan).where(MealPlan.day == day).options(selectinload(MealPlan.recipe)))
    return sorted(rows, key=lambda r: (order.index(r.meal) if r.meal in order else 9, r.id))


# ---------------------------------------------------------------- pantry


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 3}


def _stem(w: str) -> str:
    """Crude Polish stem – 'jajka'/'jajko' and 'mąki'/'mąka' meet (the word minus its ending, ≥ 3 letters)."""
    return w[: max(3, len(w) - 2)]


def at_home(name: str, pantry: list[PantryItem]) -> PantryItem | None:
    """The pantry item that looks like this ingredient name ("mąka pszenna" ↔ "Mąka"), if any."""
    stems = {_stem(w) for w in _words(name)}
    if not stems:
        return None
    for p in pantry:
        if stems & {_stem(w) for w in _words(p.name)}:
            return p
    return None


def pantry_items(db: Session) -> list[PantryItem]:
    return list(db.scalars(select(PantryItem)))


def pantry_status(p: PantryItem, today: date) -> str:
    """expired | soon | ok | none (no date)."""
    if p.expires_on is None:
        return "none"
    if p.expires_on < today:
        return "expired"
    if p.expires_on <= today + timedelta(days=SOON_DAYS):
        return "soon"
    return "ok"


def pantry_sorted(items: list[PantryItem]) -> list[PantryItem]:
    return sorted(items, key=lambda p: (p.expires_on is None, p.expires_on or date.max, p.name.lower()))


def expiring_for_push(db: Session, today: date) -> tuple[list[PantryItem], list[PantryItem]]:
    """(going off today/tomorrow, went off in the last week) – the morning push line."""
    rows = db.scalars(
        select(PantryItem).where(
            PantryItem.expires_on.is_not(None),
            PantryItem.expires_on >= today - timedelta(days=NOTIFY_EXPIRED_DAYS),
            PantryItem.expires_on <= today + timedelta(days=NOTIFY_DAYS),
        )
    )
    rows = pantry_sorted(list(rows))
    return [p for p in rows if p.expires_on >= today], [p for p in rows if p.expires_on < today]


# ---------------------------------------------------------------- recipe → shopping list


@dataclass
class Need:
    entry: str  # what goes to the list: "200 g mąki"
    name: str  # parsed name: "Mąki"
    have: PantryItem | None  # looks like it is at home already
    sources: list[str]  # recipe names it comes from


def needs(recipes: list[Recipe], pantry: list[PantryItem]) -> list[Need]:
    """Ingredients of the recipes, one per name, quantities added up ('250 g śmietany' twice → '500 g'),
    with what is already at home."""
    out: dict[str, Need] = {}
    qtys: dict[str, str | None] = {}
    for r in recipes:
        for line in r.ingredient_list:
            name, qty = parse_entry(line)
            if not name:
                continue
            key = name_key(name)
            if key in out:
                if r.name not in out[key].sources:
                    out[key].sources.append(r.name)
                qtys[key] = total = add_qty(qtys[key], qty)
                if total and " + " not in total:  # 'A + B' would not parse back – the first line stays
                    low = f"{name[:1].lower()}{name[1:]}"
                    out[key].entry = f"{low} {total}" if total.startswith("×") else f"{total} {low}"
                continue
            out[key] = Need(line, name, at_home(name, pantry), [r.name])
            qtys[key] = qty
    return list(out.values())


def find_recipe(db: Session, text: str) -> Recipe | None:
    t = " ".join(text.split()).lower()
    if not t:
        return None
    for r in db.scalars(select(Recipe)):
        if r.name.lower() == t:
            return r
    return None
