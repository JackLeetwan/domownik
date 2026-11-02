"""Application services: everything that combines the domain with the database."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from domownik import catalog
from domownik.domain import recurrence
from domownik.domain.status import Due, compute_due
from domownik.domain.tree import Tree
from domownik.models import (
    MEMBER_KINDS,
    BoardNote,
    BudgetCategory,
    Completion,
    Issue,
    Item,
    Measurement,
    ShoppingItem,
    Task,
    Todo,
    Transaction,
    User,
)

# ---------------------------------------------------------------- seeding


def ensure_budget_categories(db: Session) -> None:
    existing = {c.name for c in db.scalars(select(BudgetCategory))}
    for i, row in enumerate(catalog.seed("budget_categories")):
        if row["name"] not in existing:
            db.add(BudgetCategory(name=row["name"], kind=row["kind"], icon=row.get("icon"), sort_order=i))
    db.flush()


def budget_category_by_name(db: Session, name: str | None, kind: str = "planned") -> BudgetCategory | None:
    if not name:
        return None
    cat = db.scalar(select(BudgetCategory).where(BudgetCategory.name == name))
    if cat is None:
        cat = BudgetCategory(name=name, kind=kind)
        db.add(cat)
        db.flush()
    return cat


def seed_default_zones(db: Session, today: date) -> list[Item]:
    """Create the default zones. Only tasks that need no user input are added (holidays)."""
    ensure_budget_categories(db)
    tpls = catalog.templates()
    created = []
    for i, row in enumerate(catalog.seed("zones")):
        tpl = tpls[row["template"]]
        attrs = {"_suggest": row["suggest"]} if row.get("suggest") else {}
        item = Item(
            name=row["name"], kind=tpl.kind, icon=row.get("icon") or tpl.icon, sort_order=i * 10, attributes=attrs
        )
        db.add(item)
        db.flush()
        if row["template"] == "occasions":
            picks = [
                TaskPick(index=n, title=t.title, amount_gr=_tpl_amount(t), due=None)
                for n, t in enumerate(tpl.tasks)
                if t.default and not t.ask_date
            ]
            add_template_tasks(db, item, tpl, picks, today)
        created.append(item)
    db.commit()
    return created


# ---------------------------------------------------------------- templates → tasks


@dataclass
class TaskPick:
    index: int  # index into template.tasks
    title: str
    amount_gr: int | None
    due: date | None


def _tpl_amount(t: catalog.TaskTemplate) -> int | None:
    return round(t.amount * 100) if t.amount is not None else None


def add_template_tasks(
    db: Session, item: Item, tpl: catalog.ItemTemplate, picks: list[TaskPick], today: date
) -> list[Task]:
    out = []
    for pick in picks:
        t = tpl.tasks[pick.index]
        rule = dict(t.recurrence)
        due = pick.due
        if t.needs_user_date:
            if due is None:
                continue  # cannot create a birthday without a date
            rule["dates"] = [f"{due.month:02d}-{due.day:02d}"]
        rule = recurrence.validate(rule)
        if t.needs_user_date:
            due = recurrence.calendar_on_or_after(rule, today)  # next birthday, not the birth date
        elif due is None:
            due = recurrence.first_due(rule, today)
        cat = budget_category_by_name(db, t.budget, "income" if t.direction == "income" else "planned")
        task = Task(
            item_id=item.id,
            title=pick.title or t.title,
            category=t.category,
            recurrence=rule,
            lead_days=t.lead_days,
            next_due_date=due,
            amount_gr=pick.amount_gr if t.direction != "none" else None,
            direction=t.direction,
            budget_category_id=cat.id if cat else None,
        )
        db.add(task)
        out.append(task)
    db.flush()
    return out


# ---------------------------------------------------------------- status


def km_readings(db: Session, item_ids: set[int] | None = None) -> dict[int, list[tuple[date, float]]]:
    stmt = select(Measurement).where(Measurement.kind == "odometer")
    if item_ids is not None:
        stmt = stmt.where(Measurement.item_id.in_(item_ids))
    out: dict[int, list[tuple[date, float]]] = defaultdict(list)
    for m in db.scalars(stmt):
        out[m.item_id].append((m.measured_on, m.value))
    return out


def last_km(readings: list[tuple[date, float]]) -> int | None:
    return int(max(readings)[1]) if readings else None


def task_due(task: Task, today: date, readings: dict[int, list[tuple[date, float]]]) -> Due:
    return compute_due(
        task.next_due_date,
        task.lead_days,
        today,
        next_due_km=task.next_due_km,
        km_readings=readings.get(task.item_id),
    )


@dataclass
class TaskRow:
    task: Task
    due: Due
    zone_label: str


def active_tasks(db: Session, item_ids: set[int] | None = None) -> list[Task]:
    stmt = (
        select(Task)
        .where(Task.active.is_(True))
        .options(selectinload(Task.assignee), selectinload(Task.item))
        .order_by(Task.next_due_date)
    )
    if item_ids is not None:
        stmt = stmt.where(Task.item_id.in_(item_ids))
    return list(db.scalars(stmt))


def task_rows(db: Session, tree: Tree, today: date, item_ids: set[int] | None = None) -> list[TaskRow]:
    ids = item_ids if item_ids is not None else tree.visible()
    readings = km_readings(db, ids)
    rows = [TaskRow(t, task_due(t, today, readings), tree.label(t.item_id)) for t in active_tasks(db, ids)]
    rows.sort(key=lambda r: (r.due.date or date.max, r.task.title))
    return rows


@dataclass
class ZoneStats:
    overdue: int = 0
    soon: int = 0
    next_row: TaskRow | None = None
    year_cost_gr: int = 0
    task_count: int = 0
    open_issues: int = 0
    open_todos: int = 0


def zone_stats(db: Session, tree: Tree, items: list[Item], today: date) -> dict[int, ZoneStats]:
    out: dict[int, ZoneStats] = {}
    all_rows = task_rows(db, tree, today)
    year_start = date(today.year, 1, 1)
    spent: dict[int, int] = defaultdict(int)
    stmt = select(Transaction).where(Transaction.booked_on >= year_start, Transaction.amount_gr < 0)
    for tr in db.scalars(stmt):
        if tr.item_id:
            spent[tr.item_id] += -tr.amount_gr
    issues_by_item: dict[int, int] = defaultdict(int)
    for item_id in db.scalars(select(Issue.item_id).where(Issue.resolved_on.is_(None))):
        issues_by_item[item_id] += 1
    todos_by_item: dict[int, int] = defaultdict(int)
    for item_id in db.scalars(select(Todo.item_id).where(Todo.done_on.is_(None))):
        todos_by_item[item_id] += 1
    for item in items:
        sub = tree.subtree_ids(item.id)
        st = ZoneStats()
        st.open_issues = sum(issues_by_item[i] for i in sub)
        st.open_todos = sum(todos_by_item[i] for i in sub)
        for row in all_rows:
            if row.task.item_id not in sub:
                continue
            st.task_count += 1
            if row.due.status == "overdue":
                st.overdue += 1
            elif row.due.status in ("today", "soon"):
                st.soon += 1
            if row.due.date and (st.next_row is None or row.due.date < st.next_row.due.date):
                st.next_row = row
        st.year_cost_gr = sum(spent[i] for i in sub)
        out[item.id] = st
    return out


def open_todos(
    db: Session, tree: Tree, item_ids: set[int] | None = None, *, include_general: bool | None = None
) -> list[Todo]:
    """Open 'some day' todos: general ones first (no item), then by zone path.

    item_ids=None → everything (general included unless include_general=False);
    item_ids given → only those items (general only if include_general=True).
    """
    if include_general is None:
        include_general = item_ids is None
    visible = tree.visible()
    ids = visible if item_ids is None else (item_ids & visible)
    todos = [
        t
        for t in db.scalars(select(Todo).where(Todo.done_on.is_(None)))
        if (t.item_id is None and include_general) or (t.item_id is not None and t.item_id in ids)
    ]
    return sorted(
        todos, key=lambda t: (t.item_id is not None, tree.label(t.item_id) if t.item_id else "", t.sort_order, t.id)
    )


def active_notes(db: Session, today: date) -> list[BoardNote]:
    notes = [n for n in db.scalars(select(BoardNote).where(BoardNote.archived.is_(False))) if n.is_active(today)]
    return sorted(notes, key=lambda n: (not n.pinned, -(n.updated_at or n.created_at).timestamp()))


@dataclass
class Nav:
    """Everything the sidebar / bottom bar needs on every page."""

    zones: list[Item]
    members: list[Item]
    shopping: int
    notes: int
    todos: int


def nav_context(db: Session, today: date) -> Nav:
    tree = Tree.load(db)
    members = sorted(
        (i for i in tree.items.values() if i.is_member),
        key=lambda i: (MEMBER_KINDS.index(i.kind), i.sort_order, i.name),
    )
    shopping = db.scalar(
        select(func.count(ShoppingItem.id)).where(ShoppingItem.bought_on.is_(None), ShoppingItem.checked_at.is_(None))
    )
    return Nav(
        zones=[i for i in tree.roots() if i.kind != "member"],
        members=members,
        shopping=shopping or 0,
        notes=len(active_notes(db, today)),
        todos=len(open_todos(db, tree)),
    )


def subtree_issues(db: Session, tree: Tree, item_id: int) -> list[Issue]:
    ids = tree.subtree_ids(item_id)
    stmt = select(Issue).where(Issue.item_id.in_(ids)).order_by(Issue.started_on.desc(), Issue.id.desc())
    return list(db.scalars(stmt))


# ---------------------------------------------------------------- completion


def complete_task(
    db: Session,
    task: Task,
    *,
    user: User | None,
    done_on: date,
    amount_gr: int | None,
    km: int | None,
    vendor: str | None,
    notes: str | None,
) -> Completion:
    comp = Completion(
        task_id=task.id,
        due_date=task.next_due_date,
        done_on=done_on,
        km=km,
        actual_amount_gr=amount_gr,
        vendor=vendor or None,
        notes=notes or None,
        performed_by_id=user.id if user else None,
    )
    db.add(comp)
    db.flush()

    if km:
        db.add(Measurement(item_id=task.item_id, kind="odometer", measured_on=done_on, value=km))

    if amount_gr and task.direction != "none":
        signed = amount_gr if task.direction == "income" else -amount_gr
        db.add(
            Transaction(
                booked_on=done_on,
                amount_gr=signed,
                budget_category_id=task.budget_category_id,
                item_id=task.item_id,
                task_id=task.id,
                completion_id=comp.id,
                note=task.title,
                created_by_id=user.id if user else None,
            )
        )

    rule = task.recurrence
    nxt = recurrence.next_due(rule, previous_due=task.next_due_date, done_on=done_on)
    task.next_due_date = nxt
    if rule.get("km"):
        base_km = km or None
        task.next_due_km = base_km + int(rule["km"]) if base_km else None
    if nxt is None:
        task.active = False
    db.commit()
    return comp


def undo_completion(db: Session, comp: Completion) -> None:
    """Revert the last completion: restore the previous due date."""
    task = comp.task
    task.next_due_date = comp.due_date
    task.active = True
    if comp.km:
        m = db.scalar(
            select(Measurement).where(
                Measurement.item_id == task.item_id,
                Measurement.kind == "odometer",
                Measurement.measured_on == comp.done_on,
                Measurement.value == comp.km,
            )
        )
        if m:
            db.delete(m)
    db.delete(comp)
    db.commit()


# ---------------------------------------------------------------- timeline

MAX_POINTS_PER_TASK = 400


def timeline(
    db: Session, tree: Tree, today: date, start: date, end: date, item_ids: set[int] | None = None
) -> dict[str, Any]:
    ids = item_ids if item_ids is not None else tree.visible()
    readings = km_readings(db, ids)
    points: list[dict[str, Any]] = []
    groups: dict[int, dict[str, Any]] = {}

    def group_for(item_id: int) -> int:
        root = tree.root_of(item_id)
        gid = root.id if root else 0
        if gid not in groups:
            groups[gid] = {
                "id": gid,
                "content": f"{root.display_icon} {root.name}" if root else "Inne",
                "order": root.sort_order if root else 999,
            }
        return gid

    for task in active_tasks(db, ids):
        due = task_due(task, today, readings)
        label = tree.label(task.item_id)
        gid = group_for(task.item_id)
        base = {
            "task_id": task.id,
            "title": task.title,
            "zone": label,
            "icon": task.category_icon,
            "amount": task.amount_gr,
            "direction": task.direction,
            "group": gid,
        }
        if due.by_km and due.date and start <= due.date <= end:
            points.append(base | {"date": due.date.isoformat(), "kind": "forecast", "status": due.status})
        dates = recurrence.occurrences(task.recurrence, task.next_due_date, start, end)[:MAX_POINTS_PER_TASK]
        for n, d in enumerate(dates):
            if n == 0 and d == task.next_due_date:
                status = compute_due(d, task.lead_days, today).status
            else:
                status = "future"
            points.append(base | {"date": d.isoformat(), "kind": "due", "status": status})

    stmt = (
        select(Completion)
        .join(Task)
        .where(Completion.done_on >= start, Completion.done_on <= end, Task.item_id.in_(ids))
        .options(selectinload(Completion.task))
    )
    for comp in db.scalars(stmt):
        t = comp.task
        points.append(
            {
                "task_id": t.id,
                "title": t.title,
                "zone": tree.label(t.item_id),
                "icon": t.category_icon,
                "amount": comp.actual_amount_gr,
                "direction": t.direction,
                "group": group_for(t.item_id),
                "date": comp.done_on.isoformat(),
                "kind": "done",
                "status": "done",
            }
        )
    return {"points": points, "groups": sorted(groups.values(), key=lambda g: g["order"])}


def default_timeline_window(today: date) -> tuple[date, date]:
    return today - timedelta(days=14), today + timedelta(days=92)
