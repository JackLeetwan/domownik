"""📥 Do rozpoznania – photos snapped on the go (a service printout, a receipt, a policy) and described later.

On the go: one tap for the camera, then at most two taps – what it is about (the car) and what it is
(service, tyres, receipt, policy). At home the photo gets the rest in one form, and the app does the work:
ticks the service off (or creates it) with the cost and plans the next one, books a receipt in the budget,
keeps a policy with a reminder before it ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik import budget, documents, services
from domownik.domain import recurrence
from domownik.domain.tree import Tree
from domownik.models import CAPTURE_KINDS, MEMBER_KINDS, Attachment, Item, Task, Transaction, User, Warranty

# "następny raz": key → (label, rule fragment); None = no next time
NEXT: dict[str, tuple[str, dict | None]] = {
    "6m": ("za pół roku", {"every": 6, "unit": "month"}),
    "1y": ("za rok", {"every": 1, "unit": "year"}),
    "2y": ("za 2 lata", {"every": 2, "unit": "year"}),
    "4y": ("za 4 lata", {"every": 4, "unit": "year"}),
    "5y": ("za 5 lat", {"every": 5, "unit": "year"}),
    "none": ("nie przypominaj", None),
}
DEFAULT_NEXT = {"service": "1y", "tyres": "4y"}
DEFAULT_TITLE = {"service": "Serwis", "tyres": "Wymiana opon"}
QUICK_ITEMS = 6
NEXT_RAISE = 1.05  # the next one is planned a little dearer
_TYRES = re.compile(r"opon", re.I)


class InboxError(ValueError):
    """Shown to the user as it is."""


def pending(db: Session) -> list[Attachment]:
    rows = db.scalars(select(Attachment).where(Attachment.inbox.is_(True)).order_by(Attachment.created_at.desc()))
    return [a for a in rows if documents.visible(db, a)]


def count(db: Session) -> int:
    return db.scalar(select(func.count(Attachment.id)).where(Attachment.inbox.is_(True))) or 0


def item_choices(db: Session, tree: Tree) -> tuple[list[Item], list[Item]]:
    """(the few shown as big buttons, the rest): recently used first, then cars and the house, then by name."""
    last_used = dict(
        db.execute(
            select(Attachment.item_id, func.max(Attachment.created_at))
            .where(Attachment.item_id.is_not(None))
            .group_by(Attachment.item_id)
        ).all()
    )
    kind_rank = {"car": 0, "house": 1, "apartment": 1, "rental": 2, "appliance": 3}
    items = [i for i in tree.items.values() if i.kind not in MEMBER_KINDS or i.kind == "child"]

    def key(i: Item):
        used = last_used.get(i.id)
        return (used is None, -(used.timestamp()) if used else 0, kind_rank.get(i.kind, 5), tree.label(i.id))

    items.sort(key=key)
    return items[:QUICK_ITEMS], sorted(items[QUICK_ITEMS:], key=lambda i: tree.label(i.id))


def task_choices(db: Session, item_id: int | None, kind: str | None) -> tuple[list[Task], int | None]:
    """Active tasks of the item and the one this photo most likely closes (None = a new one)."""
    if item_id is None:
        return [], None
    tasks = db.scalars(
        select(Task).where(Task.item_id == item_id, Task.active.is_(True)).order_by(Task.next_due_date, Task.title)
    ).all()
    tasks = [t for t in tasks if t.category not in ("warranty", "occasion", "event")]
    if kind == "tyres":
        best = next((t for t in tasks if _TYRES.search(t.title)), None)
    elif kind == "service":
        best = next((t for t in tasks if t.category == "service" and not _TYRES.search(t.title)), None)
    else:
        best = None
    return tasks, best.id if best else None


def next_amount(cost_gr: int) -> int:
    """The planned amount of the next time: a bit more, rounded to 10 zł."""
    return int(round(cost_gr * NEXT_RAISE / 1000) * 1000)


def tag(db: Session, att: Attachment, *, item_id: int | None = None, kind: str | None = None) -> None:
    if item_id is not None:
        if db.get(Item, item_id) is None:
            raise InboxError("Nie ma takiego miejsca")
        att.item_id = item_id
    if kind is not None:
        if kind not in CAPTURE_KINDS:
            raise InboxError("Wybierz, co to jest")
        att.capture_kind = kind


@dataclass
class Done:
    message: str
    task: Task | None = None


def _item(db: Session, att: Attachment, raw) -> Item:
    item_id = int(raw) if str(raw or "").isdigit() else att.item_id
    item = db.get(Item, item_id) if item_id else None
    if item is None:
        raise InboxError("Wybierz, czego to dotyczy")
    att.item_id = item.id
    return item


def _close(att: Attachment, doc_kind: str, description: str | None) -> None:
    att.inbox = False
    att.doc_kind = doc_kind
    if description:
        att.description = description[:300]


def finish_service(
    db: Session,
    att: Attachment,
    user: User,
    *,
    item_raw,
    task_raw,
    title: str,
    done_on: date,
    amount_gr: int | None,
    next_key: str,
    plan_next: bool,
) -> Done:
    """Tick the service off (or create it) with the cost and the date, and plan the next one."""
    item = _item(db, att, item_raw)
    kind = att.capture_kind if att.capture_kind in DEFAULT_NEXT else "service"
    task = None
    if str(task_raw or "").isdigit():
        task = db.get(Task, int(task_raw))
        if task is None or task.item_id != item.id or not task.active:
            raise InboxError("Tego zadania już nie ma – wybierz inne albo „nowe”")
    if next_key not in NEXT and next_key != "keep":
        next_key = "keep"
    if task is None:
        if next_key == "keep":
            next_key = DEFAULT_NEXT[kind]
        title = (title or DEFAULT_TITLE[kind]).strip()[:200]
        task = Task(
            item_id=item.id,
            title=title,
            category="service",
            recurrence={"mode": "once"},
            next_due_date=done_on,
            lead_days=30,
            direction="expense",
            budget_category_id=budget.suggest_category(db, Tree.load(db), item.id, "service"),
        )
        db.add(task)
        db.flush()
    rule = NEXT.get(next_key, (None, None))[1] if next_key != "keep" else None
    if next_key == "none":
        task.recurrence = {"mode": "once"}
    elif rule is not None:
        task.recurrence = recurrence.validate({"mode": "interval", **rule, "anchor": "completion"})
    _close(att, "invoice", f"{task.title} – {done_on.strftime('%d.%m.%Y')}")
    comp = services.complete_task(
        db, task, user=user, done_on=done_on, amount_gr=amount_gr, km=None, vendor=None, notes=None
    )
    att.completion_id = comp.id
    if plan_next and amount_gr and task.active:
        task.amount_gr = next_amount(amount_gr)
    db.commit()
    if task.active and task.next_due_date:
        return Done(f"Zapisane. Następny raz: {task.next_due_date.strftime('%d.%m.%Y')}", task)
    return Done("Zapisane w historii", task)


def finish_receipt(
    db: Session,
    att: Attachment,
    user: User,
    *,
    item_raw,
    amount_gr: int | None,
    booked_on: date,
    category_raw,
    note: str,
) -> Done:
    item = _item(db, att, item_raw)
    if not amount_gr or amount_gr <= 0:
        raise InboxError("Wpisz kwotę z paragonu")
    category_id = int(category_raw) if str(category_raw or "").isdigit() else None
    note = (note or "").strip()[:200] or f"Paragon – {item.name}"
    db.add(
        Transaction(
            booked_on=booked_on,
            amount_gr=-amount_gr,
            budget_category_id=category_id,
            item_id=item.id,
            note=note,
            created_by_id=user.id,
        )
    )
    _close(att, "invoice", note)
    db.commit()
    return Done("Wydatek dopisany do budżetu")


def finish_policy(
    db: Session,
    att: Attachment,
    user: User,
    *,
    item_raw,
    name: str,
    ends_on: date | None,
    doc_kind: str,
    today: date,
) -> Done:
    item = _item(db, att, item_raw)
    name = (name or "").strip()[:200]
    if not name:
        raise InboxError("Nazwij dokument, np. „OC Mazda 2027”")
    w = Warranty(item_id=item.id, name=name, ends_on=ends_on, attachment_id=att.id, created_by_id=user.id)
    db.add(w)
    _close(att, doc_kind if doc_kind in ("policy", "contract") else "policy", name)
    db.flush()
    documents.sync_reminder(db, w, today)
    db.commit()
    if ends_on and w.task_id:
        return Done(f"Zapisane. Przypomnę przed końcem: {ends_on.strftime('%d.%m.%Y')}")
    return Done("Zapisane w dokumentach")


def finish_other(db: Session, att: Attachment, *, item_raw, description: str) -> Done:
    _item(db, att, item_raw)
    _close(att, att.doc_kind or "photo", (description or "").strip() or None)
    db.commit()
    return Done("Zapisane w dokumentach")
