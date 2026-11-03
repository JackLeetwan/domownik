"""Household budget of one month: plan from tasks, envelopes and monthly incomes vs. what was booked.

Plan of a category = its envelope (monthly limit, if set) + the planned amounts of all task
occurrences due that month (from the recurrence engine: series with an end, postponed and
skipped occurrences are honoured) + the monthly incomes expected that month.

Booked money ("wykonanie") are transactions. A transaction made by ticking off a task occurrence
belongs to the month the occurrence was *due* (a November bill paid on 30 October counts for
November), every other transaction to the month it was booked.
"""

from __future__ import annotations

import calendar
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from domownik.db import VIEWER
from domownik.domain import recurrence
from domownik.domain.tree import Tree
from domownik.models import (
    BUDGET_KINDS,
    BudgetCategory,
    Completion,
    Item,
    RecurringIncome,
    Task,
    Transaction,
)

WARN_RATIO = 0.9  # above 90 % of the plan the bar turns orange, above 100 % red
MAX_OCCURRENCES = 62  # per task and month – a daily task at most 31, keep a safety margin


def month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    return first, date(year, month, calendar.monthrange(year, month)[1])


def day_in_month(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def level(plan: int, actual: int) -> str:
    """Bar colour: ok (green) / warn (orange, over 90 %) / over (red, over 100 %)."""
    if actual <= 0:
        return "ok"
    if plan <= 0 or actual > plan:
        return "over"
    return "warn" if actual > plan * WARN_RATIO else "ok"


def percent(plan: int, actual: int) -> int:
    if plan <= 0:
        return 100 if actual > 0 else 0
    return max(0, min(100, round(actual * 100 / plan)))


@dataclass
class Entry:
    """One planned position of the month: a task occurrence or an expected income."""

    date: date
    title: str
    planned_gr: int  # positive
    income: bool
    status: str  # paid | due | overdue | auto | expected | received | done
    task: Task | None = None
    zone: str = ""
    completion: Completion | None = None
    tx: Transaction | None = None  # the transaction that settled it (task payment / income confirmation)
    source: RecurringIncome | None = None

    @property
    def actual_gr(self) -> int | None:
        if self.tx is None:
            return None
        return abs(self.tx.amount_gr)

    @property
    def settled(self) -> bool:
        return self.status in ("paid", "received", "done")


@dataclass
class CategoryLine:
    category: BudgetCategory | None  # None = "Bez kategorii"
    income: bool
    entries: list[Entry] = field(default_factory=list)
    txs: list[Transaction] = field(default_factory=list)  # everything booked this month in this category

    @property
    def key(self) -> str:
        return str(self.category.id) if self.category else ("none-in" if self.income else "none")

    @property
    def limit_gr(self) -> int:
        return (self.category.monthly_limit_gr or 0) if self.category else 0

    @property
    def planned_entries_gr(self) -> int:
        return sum(e.planned_gr for e in self.entries)

    @property
    def plan_gr(self) -> int:
        return self.limit_gr + self.planned_entries_gr

    @property
    def actual_gr(self) -> int:
        """Money in (income) / out (expense) – a refund in an expense category lowers it."""
        total = sum(t.amount_gr for t in self.txs)
        return total if self.income else -total

    @property
    def left_gr(self) -> int:
        return self.plan_gr - self.actual_gr

    @property
    def forecast_gr(self) -> int:
        return max(self.plan_gr, self.actual_gr)

    @property
    def level(self) -> str:
        return "ok" if self.income else level(self.plan_gr, self.actual_gr)

    @property
    def pct(self) -> int:
        return percent(self.plan_gr, self.actual_gr)

    @property
    def loose_txs(self) -> list[Transaction]:
        """Transactions not shown at a planned entry (manual expenses, shopping, unplanned costs)."""
        linked = {id(e.tx) for e in self.entries if e.tx is not None}
        return [t for t in self.txs if id(t) not in linked]

    @property
    def empty(self) -> bool:
        return not (self.entries or self.txs or self.limit_gr)


@dataclass
class Section:
    kind: str
    label: str
    lines: list[CategoryLine]

    @property
    def income(self) -> bool:
        return self.kind == "income"

    @property
    def plan_gr(self) -> int:
        return sum(ln.plan_gr for ln in self.lines)

    @property
    def actual_gr(self) -> int:
        return sum(ln.actual_gr for ln in self.lines)


@dataclass
class MonthBudget:
    year: int
    month: int
    today: date
    sections: list[Section]
    uncategorized: CategoryLine  # expenses without a category (tasks with an amount, loose transactions)
    uncategorized_income: CategoryLine
    overdue_before: list[Entry]  # unpaid occurrences of earlier months, still waiting (current month only)

    @property
    def first(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def prev(self) -> date:
        return (self.first - timedelta(days=1)).replace(day=1)

    @property
    def next(self) -> date:
        return (self.first + timedelta(days=32)).replace(day=1)

    @property
    def is_current(self) -> bool:
        return (self.year, self.month) == (self.today.year, self.today.month)

    def _lines(self, income: bool) -> list[CategoryLine]:
        out = [ln for s in self.sections if s.income == income for ln in s.lines]
        return [*out, self.uncategorized_income if income else self.uncategorized]

    @property
    def income_plan_gr(self) -> int:
        return sum(ln.plan_gr for ln in self._lines(True))

    @property
    def income_actual_gr(self) -> int:
        return sum(ln.actual_gr for ln in self._lines(True))

    @property
    def expense_plan_gr(self) -> int:
        return sum(ln.plan_gr for ln in self._lines(False))

    @property
    def expense_actual_gr(self) -> int:
        return sum(ln.actual_gr for ln in self._lines(False))

    @property
    def expense_left_gr(self) -> int:
        return self.expense_plan_gr - self.expense_actual_gr

    @property
    def remaining_gr(self) -> int:
        """Forecast for the end of the month: in each category the larger of plan and actual."""
        return sum(ln.forecast_gr for ln in self._lines(True)) - sum(ln.forecast_gr for ln in self._lines(False))

    @property
    def balance_now_gr(self) -> int:
        return self.income_actual_gr - self.expense_actual_gr

    @property
    def expense_level(self) -> str:
        return level(self.expense_plan_gr, self.expense_actual_gr)

    @property
    def expense_pct(self) -> int:
        return percent(self.expense_plan_gr, self.expense_actual_gr)

    @property
    def income_pct(self) -> int:
        return percent(self.income_plan_gr, self.income_actual_gr)

    def line(self, category_id: int | None) -> CategoryLine | None:
        for ln in self._lines(True) + self._lines(False):
            if (ln.category.id if ln.category else None) == category_id:
                return ln
        return None

    def entries_on(self, d: date) -> list[Entry]:
        return [e for ln in self._lines(True) + self._lines(False) for e in ln.entries if e.date == d]

    @property
    def unassigned_tasks(self) -> list[Task]:
        """Tasks with an amount but without a category, planned this month – offered for assignment."""
        seen: dict[int, Task] = {}
        for ln in (self.uncategorized, self.uncategorized_income):
            for e in ln.entries:
                if e.task is not None and e.task.budget_category_id is None:
                    seen.setdefault(e.task.id, e.task)
        return list(seen.values())


def _signed(task: Task) -> int:
    return -1 if task.direction == "expense" else 1


def _month_of_tx(tx: Transaction) -> date:
    comp = tx.completion
    if comp is not None:
        return comp.due_date or comp.done_on
    return tx.booked_on


def month_transactions(db: Session, first: date, last: date) -> list[Transaction]:
    """Transactions belonging to the month (task payments by the due date of their occurrence)."""
    comp_ids = select(Completion.id).where(
        or_(
            Completion.due_date.between(first, last),
            Completion.due_date.is_(None) & Completion.done_on.between(first, last),
        )
    )
    stmt = (
        select(Transaction)
        .where(
            or_(
                Transaction.completion_id.in_(comp_ids),
                Transaction.completion_id.is_(None) & Transaction.booked_on.between(first, last),
                # paid in this month, but for an occurrence of another one – filtered below
                Transaction.completion_id.is_not(None) & Transaction.booked_on.between(first, last),
            )
        )
        .options(
            selectinload(Transaction.completion),
            selectinload(Transaction.created_by),
            selectinload(Transaction.task),
            selectinload(Transaction.budget_category),
        )
        .order_by(Transaction.booked_on, Transaction.id)
    )
    return [t for t in db.scalars(stmt) if first <= _month_of_tx(t) <= last]


def _occurrence_status(task: Task, d: date, today: date) -> str:
    if d < today:
        return "overdue"
    return "auto" if task.autopay else "due"


PRIVATE_TITLE = "🔒 Prywatne"


def compute_month(db: Session, year: int, month: int, today: date, tree: Tree | None = None) -> MonthBudget:
    first, last = month_bounds(year, month)
    tree = tree or Tree.load(db, include_archived=True)
    cats = {c.id: c for c in db.scalars(select(BudgetCategory).order_by(BudgetCategory.sort_order, BudgetCategory.id))}
    lines: dict[int | None, CategoryLine] = {}
    none_in = CategoryLine(None, income=True)
    none_out = CategoryLine(None, income=False)

    def line_for(cat_id: int | None, income: bool) -> CategoryLine:
        cat = cats.get(cat_id) if cat_id is not None else None
        if cat is None:
            return none_in if income else none_out
        if cat.id not in lines:
            lines[cat.id] = CategoryLine(cat, income=cat.is_income)
        return lines[cat.id]

    # every non-archived category is shown, even with nothing planned yet (so a limit can be seen/set)
    for c in cats.values():
        if not c.archived:
            line_for(c.id, c.is_income)

    txs = month_transactions(db, first, last)
    tx_by_comp = {t.completion_id: t for t in txs if t.completion_id}
    tx_by_income: dict[int, Transaction] = {}
    for t in txs:
        if t.income_id:
            tx_by_income.setdefault(t.income_id, t)
        line_for(t.budget_category_id, t.amount_gr > 0 if t.budget_category_id is None else False).txs.append(t)

    visible = Tree.load(db).visible()  # tasks of archived items are not planned (like on the dashboard)
    overdue_before: list[Entry] = []

    # 1) occurrences already ticked off (done / paid / booked automatically) that were due this month
    comp_stmt = (
        select(Completion)
        .where(
            or_(
                Completion.due_date.between(first, last),
                Completion.due_date.is_(None) & Completion.done_on.between(first, last),
            )
        )
        .options(selectinload(Completion.task).selectinload(Task.item))
        .execution_options(all_private=True)  # the budget is shared: every amount counts (titles masked below)
    )
    for comp in db.scalars(comp_stmt):
        task = comp.task
        tx = tx_by_comp.get(comp.id)
        if task.direction == "none" or (not task.amount_gr and tx is None):
            continue
        income = task.direction == "income"
        cat_id = tx.budget_category_id if tx is not None else task.budget_category_id
        entry = Entry(
            date=comp.due_date or comp.done_on,
            title=task.full_title,
            planned_gr=task.amount_gr or 0,
            income=income,
            status=("received" if income else "paid") if tx is not None else "done",
            task=task,
            zone=tree.label(task.item_id),
            completion=comp,
            tx=tx,
        )
        line_for(cat_id, income).entries.append(entry)

    # 2) occurrences still ahead (or overdue) – the recurrence engine projects the rest of each series
    task_stmt = (
        select(Task)
        .where(
            Task.active.is_(True),
            Task.amount_gr.is_not(None),
            Task.amount_gr != 0,
            Task.direction != "none",
            Task.next_due_date.is_not(None),
            Task.next_due_date <= last,
        )
        .options(selectinload(Task.item))
        .execution_options(all_private=True)
    )
    for task in db.scalars(task_stmt):
        if task.item_id not in visible:
            continue
        income = task.direction == "income"
        due = task.next_due_date
        if due < first and (year, month) == (today.year, today.month):
            overdue_before.append(
                Entry(due, task.full_title, task.amount_gr, income, "overdue", task, tree.label(task.item_id))
            )
        try:
            dates = recurrence.occurrences(task.recurrence, due, min(first, due), last)
        except recurrence.RuleError:
            dates = [due] if first <= due <= last else []
        for n, d in enumerate(dates[: MAX_OCCURRENCES + 1]):
            if d < first:
                continue
            line_for(task.budget_category_id, income).entries.append(
                Entry(
                    date=d,
                    title=task.title_at(n),
                    planned_gr=task.amount_gr,
                    income=income,
                    status=_occurrence_status(task, d, today),
                    task=task,
                    zone=tree.label(task.item_id),
                )
            )

    # 3) monthly incomes expected this month
    for inc in db.scalars(select(RecurringIncome).order_by(RecurringIncome.sort_order, RecurringIncome.id)):
        if not inc.expected_in(first, last):
            continue
        tx = tx_by_income.get(inc.id)
        line_for(inc.budget_category_id, True).entries.append(
            Entry(
                date=day_in_month(year, month, inc.day),
                title=inc.name,
                planned_gr=inc.amount_gr,
                income=True,
                status="received" if tx is not None else "expected",
                tx=tx,
                source=inc,
            )
        )

    for ln in [*lines.values(), none_in, none_out]:
        ln.entries.sort(key=lambda e: (e.date, e.title))

    sections = []
    for kind, label in BUDGET_KINDS.items():
        ls = [ln for ln in lines.values() if ln.category.kind == kind]
        # archived categories show up only when something happened in them this month
        ls = [ln for ln in ls if not ln.category.archived or not ln.empty]
        sections.append(Section(kind, label, ls))
    overdue_before.sort(key=lambda e: (e.date, e.title))
    # someone else's private task: the amount counts, what it is stays private
    viewer = db.info.get(VIEWER)
    if VIEWER in db.info:
        for e in [
            *overdue_before,
            *(e for ln in lines.values() for e in ln.entries),
            *none_out.entries,
            *none_in.entries,
        ]:
            if e.task is not None and e.task.private_to_id not in (None, viewer):
                e.title, e.task, e.zone = PRIVATE_TITLE, None, ""
    return MonthBudget(year, month, today, sections, none_out, none_in, overdue_before)


# ---------------------------------------------------------------- helpers for forms


def active_categories(db: Session, *, income: bool | None = None) -> list[BudgetCategory]:
    stmt = select(BudgetCategory).where(BudgetCategory.archived.is_(False))
    cats = list(db.scalars(stmt.order_by(BudgetCategory.sort_order, BudgetCategory.id)))
    if income is None:
        return cats
    return [c for c in cats if c.is_income == income]


def last_used_category(db: Session, user_id: int) -> int | None:
    """Category of this person's last manually entered expense (the default of the quick form)."""
    stmt = (
        select(Transaction.budget_category_id)
        .join(BudgetCategory, Transaction.budget_category_id == BudgetCategory.id)
        .where(
            Transaction.created_by_id == user_id,
            Transaction.task_id.is_(None),
            Transaction.income_id.is_(None),
            Transaction.amount_gr < 0,
            BudgetCategory.archived.is_(False),
        )
        .order_by(Transaction.id.desc())
        .limit(1)
    )
    return db.scalar(stmt)


# task category / item kind → name of the seeded budget category
SUGGEST_BY_CATEGORY = {
    "insurance": "Ubezpieczenia",
    "tax": "Podatki",
    "health": "Zdrowie",
    "occasion": "Prezenty i okazje",
    "shopping": "Wydatki nieregularne",
}
SUGGEST_BY_KIND = {
    "car": "Samochody",
    "appliance": "AGD i urządzenia",
    "house": "Dom",
    "apartment": "Dom",
    "rental": "Mieszkanie na wynajem",
}


def suggest_category(db: Session, tree: Tree, item_id: int | None, task_category: str | None) -> int | None:
    """Best guess for a task without a budget category.

    1. what other tasks of the same kind in the same zone use, 2. the seeded category for the kind
    of task (insurance, tax…), 3. the one for the kind of item (car, appliance, house…).
    """
    names = {c.name: c.id for c in active_categories(db)}
    active_ids = set(names.values())
    if item_id is not None and item_id in tree.items:
        root = tree.root_of(item_id)
        if root is not None:
            zone = tree.subtree_ids(root.id)
            votes = Counter(
                t.budget_category_id
                for t in db.scalars(
                    select(Task).where(
                        Task.item_id.in_(zone), Task.category == task_category, Task.budget_category_id.is_not(None)
                    )
                )
                if t.budget_category_id in active_ids
            )
            if votes:
                return votes.most_common(1)[0][0]
    if task_category in SUGGEST_BY_CATEGORY and SUGGEST_BY_CATEGORY[task_category] in names:
        return names[SUGGEST_BY_CATEGORY[task_category]]
    if task_category == "income":
        return None
    node: Item | None = tree.items.get(item_id) if item_id is not None else None
    while node is not None:
        name = SUGGEST_BY_KIND.get(node.kind)
        if name in names:
            return names[name]
        node = tree.items.get(node.parent_id) if node.parent_id else None
    return None


def assign_task_category(db: Session, task: Task, category_id: int | None) -> None:
    """Set the budget category of a task; its uncategorised past payments move along."""
    task.budget_category_id = category_id
    if category_id is not None:
        for tx in db.scalars(
            select(Transaction).where(Transaction.task_id == task.id, Transaction.budget_category_id.is_(None))
        ):
            tx.budget_category_id = category_id


def confirm_income(
    db: Session, inc: RecurringIncome, year: int, month: int, *, amount_gr: int | None, today: date, user_id: int | None
) -> Transaction:
    """ "✓ Wpłynęło": book the income of that month (the planned amount unless another is given)."""
    first, last = month_bounds(year, month)
    existing = db.scalar(
        select(Transaction).where(Transaction.income_id == inc.id, Transaction.booked_on.between(first, last))
    )
    amount = abs(amount_gr) if amount_gr else inc.amount_gr
    if existing is not None:
        existing.amount_gr = amount
        return existing
    booked = day_in_month(year, month, inc.day)
    if booked > today and first <= today <= last:
        booked = today  # confirmed early: it came in today
    tx = Transaction(
        booked_on=booked,
        amount_gr=amount,
        budget_category_id=inc.budget_category_id,
        income_id=inc.id,
        item_id=inc.item_id,
        note=inc.name,
        created_by_id=user_id,
    )
    db.add(tx)
    return tx


def set_transaction_amount(tx: Transaction, amount_gr: int) -> None:
    """Change the amount, keeping the direction; a task payment updates its completion too."""
    sign = -1 if tx.amount_gr < 0 else 1
    tx.amount_gr = sign * abs(amount_gr)
    if tx.completion is not None:
        tx.completion.actual_amount_gr = abs(amount_gr)


def end_income(db: Session, inc: RecurringIncome, today: date) -> bool:
    """Stop expecting an income. Never confirmed → deleted; otherwise ends after the last month it came in.

    Returns True when it was deleted.
    """
    confirmed = list(db.scalars(select(Transaction).where(Transaction.income_id == inc.id)))
    if not confirmed:
        db.delete(inc)
        return True
    first, last = month_bounds(today.year, today.month)
    this_month = any(first <= t.booked_on <= last for t in confirmed)
    inc.ends_on = last if this_month else first - timedelta(days=1)
    return False
