"""Yearly costs of a zone: how much the car, the house or the rented flat cost this year (and brought in).

Everything booked in the budget counts: task payments, hand entries and bank imports with a zone, and monthly
incomes assigned to a zone (rent from a tenant). Money put into savings goals is not a cost.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik.domain.tree import Tree
from domownik.models import BudgetCategory, Item, RecurringIncome, Task, Transaction


@dataclass
class Part:
    name: str
    icon: str
    gr: int


@dataclass
class ZoneYear:
    item: Item | None  # None = not assigned to any zone
    expense_gr: int = 0
    income_gr: int = 0
    prev_expense_gr: int = 0
    months: list[int] = field(default_factory=lambda: [0] * 12)  # expenses per month
    by_category: list[Part] = field(default_factory=list)
    by_item: list[Part] = field(default_factory=list)

    @property
    def net_gr(self) -> int:
        return self.income_gr - self.expense_gr

    @property
    def month_max(self) -> int:
        return max(self.months) or 1

    @property
    def monthly_avg_gr(self) -> int:
        return self.expense_gr // 12

    @property
    def change_pct(self) -> int | None:
        if not self.prev_expense_gr:
            return None
        return round((self.expense_gr - self.prev_expense_gr) * 100 / self.prev_expense_gr)


def _zone_item_of(tx: Transaction, tasks: dict[int, int], incomes: dict[int, int | None]) -> int | None:
    if tx.item_id is not None:
        return tx.item_id
    if tx.task_id is not None:
        return tasks.get(tx.task_id)
    if tx.income_id is not None:
        return incomes.get(tx.income_id)
    return None


def year_costs(db: Session, year: int, tree: Tree | None = None) -> tuple[list[ZoneYear], ZoneYear]:
    """Zones with any money this year or the year before (biggest cost first), and the unassigned rest."""
    tree = tree or Tree.load(db, include_archived=True)
    first, last = date(year - 1, 1, 1), date(year, 12, 31)
    txs = list(
        db.scalars(
            select(Transaction)
            .where(Transaction.booked_on.between(first, last), Transaction.goal_id.is_(None))
            .execution_options(all_private=True)
        )
    )
    task_item = dict(db.execute(select(Task.id, Task.item_id).execution_options(all_private=True)).all())
    income_item = dict(db.execute(select(RecurringIncome.id, RecurringIncome.item_id)).all())
    cats = {c.id: c for c in db.scalars(select(BudgetCategory))}

    zones: dict[int | None, ZoneYear] = {}
    cat_sum: dict[int | None, dict[int | None, int]] = defaultdict(lambda: defaultdict(int))
    item_sum: dict[int | None, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for tx in txs:
        item_id = _zone_item_of(tx, task_item, income_item)
        root = tree.root_of(item_id) if item_id is not None and item_id in tree.items else None
        key = root.id if root is not None else None
        z = zones.setdefault(key, ZoneYear(root))
        if tx.booked_on.year == year - 1:
            if tx.amount_gr < 0:
                z.prev_expense_gr += -tx.amount_gr
            continue
        if tx.amount_gr > 0:
            z.income_gr += tx.amount_gr
            continue
        cost = -tx.amount_gr
        z.expense_gr += cost
        z.months[tx.booked_on.month - 1] += cost
        cat_sum[key][tx.budget_category_id] += cost
        if item_id is not None and root is not None and item_id != root.id:
            # the costs of a car / appliance inside the zone: under its direct child of the root
            path = tree.path(item_id)
            item_sum[key][path[1].id if len(path) > 1 else item_id] += cost
    for key, z in zones.items():
        z.by_category = sorted(
            (
                Part(cats[c].name if c in cats else "Bez kategorii", (cats[c].icon or "") if c in cats else "", gr)
                for c, gr in cat_sum[key].items()
            ),
            key=lambda p: -p.gr,
        )
        z.by_item = sorted(
            (Part(tree.items[i].name, tree.items[i].display_icon, gr) for i, gr in item_sum[key].items()),
            key=lambda p: -p.gr,
        )
    rest = zones.pop(None, ZoneYear(None))
    ordered = sorted(
        (z for z in zones.values() if z.expense_gr or z.income_gr or z.prev_expense_gr),
        key=lambda z: (-z.expense_gr, z.item.name if z.item else ""),
    )
    return ordered, rest


def zone_year_total(db: Session, item_id: int, year: int) -> ZoneYear | None:
    """One zone's year (for the tile on its page); None when nothing was booked."""
    zones, _ = year_costs(db, year)
    return next((z for z in zones if z.item is not None and z.item.id == item_id), None)
