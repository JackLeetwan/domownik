"""📥 Do rozpoznania – photos snapped on the go (a service printout, a receipt, a policy) and described later.

On the go: one tap for the camera, then at most two taps – what it is about (the car) and what it is
(service, tyres, receipt, policy). At home the photo gets the rest in one form, and the app does the work:
ticks the service off (or creates it) with the cost and plans the next one, books a receipt in the budget,
keeps a policy with a reminder before it ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik import budget, documents, services
from domownik.domain import recurrence
from domownik.domain.tree import Tree
from domownik.models import (
    CAPTURE_KINDS,
    MEMBER_KINDS,
    Attachment,
    Completion,
    Item,
    Task,
    Transaction,
    User,
    Warranty,
)

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
MATCH_AHEAD_DAYS = 60  # a task due within two months (or overdue) is what a photo most likely closes
RECENT_DAYS = 14  # a completion this close to the photo: the photo is its paper
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


@dataclass
class Choices:
    """What the photo can close: the item's tasks, completions from around the photo's day, the preselected one.

    Option values: "12" = tick task 12 off, "c5" = the photo is the paper of completion 5 (already ticked off),
    "d12" = only file the document with task 12 (policies), "" = a new one.
    """

    tasks: list[Task]
    recent: list[Completion]
    best: str


def _fits(task: Task, kind: str | None) -> bool:
    if kind == "tyres":
        return bool(_TYRES.search(task.title))
    if kind == "service":
        return task.category in ("service", "inspection") and not _TYRES.search(task.title)
    if kind == "policy":
        return task.category == "insurance"
    return False


def task_choices(db: Session, item_id: int | None, kind: str | None, taken_on: date) -> Choices:
    """The task the photo most likely closes is one due around the day it was taken – not just the first one:
    a tyre receipt in November must not tick off next April's swap. A service ticked off a few days ago gets
    the photo attached instead of being ticked off twice."""
    if item_id is None:
        return Choices([], [], "")
    tasks = db.scalars(
        select(Task).where(Task.item_id == item_id, Task.active.is_(True)).order_by(Task.next_due_date, Task.title)
    ).all()
    if kind == "policy":
        tasks = [t for t in tasks if t.category == "insurance"]
    else:
        tasks = [t for t in tasks if t.category not in ("warranty", "occasion", "event", "insurance")]
    recent = [
        c
        for c in db.scalars(
            select(Completion)
            .join(Task, Completion.task_id == Task.id)
            .where(
                Task.item_id == item_id,
                Completion.done_on >= taken_on - timedelta(days=RECENT_DAYS),
                Completion.done_on <= taken_on + timedelta(days=RECENT_DAYS),
            )
        ).all()
        if _fits(c.task, kind)
    ]
    recent.sort(key=lambda c: abs((c.done_on - taken_on).days))
    if recent:
        return Choices(tasks, recent, f"c{recent[0].id}")
    due = [
        t
        for t in tasks
        if _fits(t, kind) and t.next_due_date and t.next_due_date <= taken_on + timedelta(days=MATCH_AHEAD_DAYS)
    ]
    if due:
        best = min(due, key=lambda t: abs((t.next_due_date - taken_on).days))
        return Choices(tasks, [], str(best.id))
    if kind == "policy" and tasks:  # mid-term: most likely the current policy, filed with its task
        return Choices(tasks, [], f"d{tasks[0].id}")
    return Choices(tasks, [], "")


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


def _target(db: Session, raw, item: Item) -> tuple[str, Task | None, Completion | None]:
    """The option picked in "Co zostało zrobione" / "Której polisy": ("tick" | "attach" | "file" | "new", ...)."""
    raw = str(raw or "").strip()
    if not raw:
        return "new", None, None
    mode, num = {"c": "attach", "d": "file"}.get(raw[0], "tick"), raw.lstrip("cd")
    if not num.isdigit():
        return "new", None, None
    if mode == "attach":
        comp = db.get(Completion, int(num))
        if comp is None or comp.task is None or comp.task.item_id != item.id:
            raise InboxError("Tego wykonania już nie ma – wybierz inne albo „nowe”")
        return mode, comp.task, comp
    task = db.get(Task, int(num))
    if task is None or task.item_id != item.id or not task.active:
        raise InboxError("Tego zadania już nie ma – wybierz inne albo „nowe”")
    return mode, task, None


def _set_next(task: Task, next_key: str) -> None:
    """A changed "Następny raz" becomes the task's rule (counted from the day it was done)."""
    if next_key == "none":
        task.recurrence = {"mode": "once"}
    elif next_key in NEXT:
        task.recurrence = recurrence.validate({"mode": "interval", **NEXT[next_key][1], "anchor": "completion"})


def _attach(db: Session, att: Attachment, task: Task, comp: Completion, user: User, amount_gr: int | None) -> None:
    """The photo is the paper of a completion that is already there: no second tick, the cost only if missing."""
    att.completion_id = comp.id
    if amount_gr and not comp.actual_amount_gr:
        comp.actual_amount_gr = amount_gr
        services.book_cost(db, task, comp, amount_gr, user)


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
    """Tick the service off (or create it) with the cost and the date and plan the next one – or, when it was
    already ticked off, attach the photo to that."""
    item = _item(db, att, item_raw)
    kind = att.capture_kind if att.capture_kind in DEFAULT_NEXT else "service"
    mode, task, comp = _target(db, task_raw, item)
    if next_key not in NEXT:
        next_key = "keep"
    if mode == "attach":
        _close(att, "invoice", f"{task.title} – {comp.done_on.strftime('%d.%m.%Y')}")
        _attach(db, att, task, comp, user, amount_gr)
        if next_key != "keep":
            _set_next(task, next_key)
            task.recurrence, task.next_due_date = recurrence.advance(
                task.recurrence, previous_due=comp.due_date, done_on=comp.done_on
            )
            task.active = task.next_due_date is not None
        if plan_next and amount_gr and task.active:
            task.amount_gr = next_amount(amount_gr)
        db.commit()
        return Done(f"Zdjęcie dołączone do: {task.title} ({comp.done_on.strftime('%d.%m.%Y')})", task)
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
    _set_next(task, next_key)
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
    task_raw,
    name: str,
    ends_on: date | None,
    amount_gr: int | None,
    done_on: date,
    doc_kind: str,
    today: date,
) -> Done:
    """A policy of an insurance the app already knows renews it (or is filed with it) – no second reminder.
    Only a new one becomes a document with a reminder before it ends."""
    item = _item(db, att, item_raw)
    name = (name or "").strip()[:200]
    doc_kind = doc_kind if doc_kind in ("policy", "contract") else "policy"
    mode, task, comp = _target(db, task_raw, item)
    if mode == "attach":
        _close(att, doc_kind, name or task.title)
        _attach(db, att, task, comp, user, amount_gr)
        db.commit()
        return Done(f"Polisa dołączona do: {task.title}")
    if mode == "file":
        _close(att, doc_kind, name or task.title)
        att.task_id = task.id
        if ends_on:
            task.next_due_date = ends_on
        db.commit()
        return Done(f"Polisa dołączona do: {task.title}")
    if mode == "tick":
        _close(att, doc_kind, name or task.title)
        comp = services.complete_task(
            db, task, user=user, done_on=done_on, amount_gr=amount_gr, km=None, vendor=None, notes=name or None
        )
        att.completion_id = comp.id
        if task.active and ends_on:
            task.next_due_date = ends_on
        if amount_gr and task.active:
            task.amount_gr = amount_gr
        db.commit()
        if task.active and task.next_due_date:
            return Done(f"Odnowione. Następne odnowienie: {task.next_due_date.strftime('%d.%m.%Y')}")
        return Done("Zapisane w historii")
    if not name:
        raise InboxError("Nazwij dokument, np. „OC 2027”")
    w = Warranty(item_id=item.id, name=name, ends_on=ends_on, attachment_id=att.id, created_by_id=user.id)
    db.add(w)
    _close(att, doc_kind, name)
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
