"""Savings goals ("Wakacje 2027: 6000 zł"): progress from budget transactions marked with goal_id.

A deposit is an expense of the month in the "Oszczędności" category (money leaves the spending pot),
a withdrawal gives it back. Nothing is stored twice: the saved amount is the sum of the goal's transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik.models import BudgetCategory, SavingsGoal, Transaction, User

SAVINGS_CATEGORY = "Oszczędności"


def savings_category(db: Session) -> BudgetCategory:
    """The budget category deposits go to – created on first use, revived if it was archived."""
    cat = db.scalar(
        select(BudgetCategory).where(BudgetCategory.name == SAVINGS_CATEGORY, BudgetCategory.parent_id.is_(None))
    )
    if cat is None:
        top = db.scalar(select(func.max(BudgetCategory.sort_order))) or 0
        cat = BudgetCategory(name=SAVINGS_CATEGORY, kind="planned", icon="🐷", sort_order=top + 1)
        db.add(cat)
        db.flush()
    cat.archived = False
    return cat


def months_between(a: date, b: date) -> int:
    """Whole months from a's month to b's month, counting the current one (≥ 1)."""
    return max(1, (b.year - a.year) * 12 + (b.month - a.month) + 1)


@dataclass
class GoalView:
    goal: SavingsGoal
    saved_gr: int

    @property
    def percent(self) -> int:
        return min(100, round(100 * self.saved_gr / self.goal.target_gr)) if self.goal.target_gr > 0 else 100

    @property
    def left_gr(self) -> int:
        return max(0, self.goal.target_gr - self.saved_gr)

    @property
    def reached(self) -> bool:
        return self.saved_gr >= self.goal.target_gr

    def monthly_gr(self, today: date) -> int | None:
        """How much to put aside each month (this one included) to make it by the due date."""
        g = self.goal
        if g.due_on is None or self.reached or g.due_on < today:
            return None
        n = months_between(today, g.due_on)
        return -(-self.left_gr // n)  # round up to the grosz

    def late(self, today: date) -> bool:
        return self.goal.due_on is not None and self.goal.due_on < today and not self.reached


def saved_amounts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(Transaction.goal_id, func.sum(Transaction.amount_gr))
        .where(Transaction.goal_id.is_not(None))
        .group_by(Transaction.goal_id)
    )
    return {gid: -(total or 0) for gid, total in rows}


def views(db: Session, *, done: bool = False) -> list[GoalView]:
    stmt = select(SavingsGoal).where(SavingsGoal.done_on.is_not(None) if done else SavingsGoal.done_on.is_(None))
    goals = db.scalars(
        stmt.order_by(SavingsGoal.sort_order, SavingsGoal.due_on.is_(None), SavingsGoal.due_on, SavingsGoal.id)
    )
    saved = saved_amounts(db)
    return [GoalView(g, saved.get(g.id, 0)) for g in goals]


def move(
    db: Session, goal: SavingsGoal, amount_gr: int, *, user: User, on: date, withdraw: bool = False
) -> Transaction:
    """Put money into the goal (or take it back). The caller commits."""
    if amount_gr <= 0:
        raise ValueError("Podaj kwotę większą od zera")
    if withdraw:
        saved = saved_amounts(db).get(goal.id, 0)
        if amount_gr > saved:
            raise ValueError("Nie można wypłacić więcej, niż jest odłożone")
    tx = Transaction(
        booked_on=on,
        amount_gr=amount_gr if withdraw else -amount_gr,
        budget_category_id=savings_category(db).id,
        goal_id=goal.id,
        note=("Wypłata z celu: " if withdraw else "Na cel: ") + goal.name,
        created_by_id=user.id,
    )
    db.add(tx)
    return tx
