"""Points for tasks and chores, spent on rewards.

Earned with ✓ Zrobione (task.points / chore.points) by the person who did it. With needs_approval the
points wait ("pending") until someone *else* confirms them; a reward taken is pending the same way.
Undoing the ✓ removes the points with it (FK cascade on the completion / chore_done row).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik.models import Chore, ChoreDone, Completion, PointEntry, Reward, Task, User

MAX_POINTS = 10_000


def parse_points(raw) -> int | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    if not raw.lstrip("-").isdigit():
        raise ValueError("Punkty to liczba całkowita, np. 10")
    n = int(raw)
    if not 0 <= n <= MAX_POINTS:
        raise ValueError(f"Punkty od 0 do {MAX_POINTS}")
    return n or None


def for_task(db: Session, task: Task, comp: Completion, user: User | None) -> PointEntry | None:
    if not task.points or user is None:
        return None
    e = PointEntry(
        user_id=user.id,
        points=task.points,
        reason=task.full_title,
        status="pending" if task.needs_approval else "approved",
        task_id=task.id,
        completion_id=comp.id,
        created_by_id=user.id,
    )
    db.add(e)
    return e


def for_chore(db: Session, chore: Chore, rec: ChoreDone, by: User, actor: User) -> PointEntry | None:
    if not chore.points:
        return None
    db.flush()  # rec.id
    e = PointEntry(
        user_id=by.id,
        points=chore.points,
        reason=f"🧹 {chore.name}",
        status="pending" if chore.needs_approval else "approved",
        chore_done_id=rec.id,
        created_by_id=actor.id,
    )
    db.add(e)
    return e


@dataclass
class Balance:
    user: User
    approved: int  # what counts
    pending_plus: int  # waiting to be confirmed
    pending_minus: int  # rewards asked for, not confirmed yet

    @property
    def available(self) -> int:
        """What can still be spent: confirmed points minus rewards already asked for."""
        return self.approved - self.pending_minus


def balances(db: Session, users: list[User]) -> list[Balance]:
    rows = db.execute(
        select(PointEntry.user_id, PointEntry.status, PointEntry.points > 0, func.sum(PointEntry.points))
        .where(PointEntry.status.in_(("approved", "pending")))
        .group_by(PointEntry.user_id, PointEntry.status, PointEntry.points > 0)
    )
    acc: dict[int, list[int]] = {u.id: [0, 0, 0] for u in users}
    for uid, status, positive, total in rows:
        if uid not in acc:
            continue
        if status == "approved":
            acc[uid][0] += total or 0
        elif positive:
            acc[uid][1] += total or 0
        else:
            acc[uid][2] += -(total or 0)
    return [Balance(u, *acc[u.id]) for u in users]


def balance_of(db: Session, user: User) -> Balance:
    return balances(db, [user])[0]


def redeem(db: Session, reward: Reward, user: User) -> PointEntry:
    """Ask for a reward – it waits for someone else's ✓, the points are held meanwhile."""
    if not reward.active:
        raise ValueError("Ta nagroda jest wyłączona")
    if balance_of(db, user).available < reward.cost:
        raise ValueError("Za mało punktów na tę nagrodę")
    e = PointEntry(
        user_id=user.id,
        points=-reward.cost,
        reason=f"{reward.icon or '🎁'} {reward.name}",
        status="pending",
        reward_id=reward.id,
        created_by_id=user.id,
    )
    db.add(e)
    return e


def decide(entry: PointEntry, by: User, *, approve: bool) -> None:
    if entry.status != "pending":
        raise ValueError("To już zostało rozstrzygnięte")
    if entry.user_id == by.id:
        raise ValueError("Własne punkty i nagrody zatwierdza ktoś inny z domowników")
    entry.status = "approved" if approve else "rejected"
    entry.decided_by_id = by.id


def pending_for(db: Session, user: User) -> list[PointEntry]:
    """What this user may confirm: everyone else's pending entries."""
    return list(
        db.scalars(
            select(PointEntry)
            .where(PointEntry.status == "pending", PointEntry.user_id != user.id)
            .order_by(PointEntry.created_at)
        )
    )


def pending_count(db: Session, user: User) -> int:
    return (
        db.scalar(
            select(func.count(PointEntry.id)).where(PointEntry.status == "pending", PointEntry.user_id != user.id)
        )
        or 0
    )


def in_use(db: Session) -> bool:
    """Show the points page in the menu once anything gives points or a reward exists."""
    return bool(
        db.scalar(select(func.count(Task.id)).where(Task.points.is_not(None)))
        or db.scalar(select(func.count(Chore.id)).where(Chore.points.is_not(None)))
        or db.scalar(select(func.count(Reward.id)))
        or db.scalar(select(func.count(PointEntry.id)))
    )
