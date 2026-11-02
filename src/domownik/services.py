"""Application services: everything that combines the domain with the database."""

from __future__ import annotations

from collections import Counter, defaultdict
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
        # a household member linked to an account also "owns" the tasks assigned to that account
        owner = item.user_id if item.is_member else None
        for row in all_rows:
            if row.task.item_id not in sub and (owner is None or row.task.assignee_id != owner):
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
                note=task.full_title,
                created_by_id=user.id if user else None,
            )
        )

    rule = task.recurrence
    task.recurrence, nxt = recurrence.advance(rule, previous_due=task.next_due_date, done_on=done_on)
    task.next_due_date = nxt
    if rule.get("km"):
        base_km = km or None
        task.next_due_km = base_km + int(rule["km"]) if base_km else None
    if nxt is None:
        task.active = False
    db.commit()
    return comp


AUTOPAY_NOTE = "Zapłacone automatycznie"
MAX_AUTOPAY_CATCHUP = 400  # safety net for a rule that never moves forward


def settle_autopay(db: Session, today: date) -> int:
    """Book standing orders / direct debits whose due date has come.

    Each due occurrence becomes a completion on its due date with the planned amount
    (so it lands in the history and the budget), then the task moves to the next date.
    """
    stmt = (
        select(Task)
        .join(Item, Task.item_id == Item.id)
        .where(
            Task.active.is_(True),
            Task.autopay.is_(True),
            Task.next_due_date.is_not(None),
            Task.next_due_date <= today,
            Item.archived.is_(False),
        )
    )
    booked = 0
    for task in list(db.scalars(stmt)):
        for _ in range(MAX_AUTOPAY_CATCHUP):
            due = task.next_due_date
            if not task.active or due is None or due > today:
                break
            complete_task(
                db,
                task,
                user=None,
                done_on=due,
                amount_gr=task.amount_gr,
                km=None,
                vendor=None,
                notes=AUTOPAY_NOTE,
            )
            booked += 1
            if task.next_due_date == due:
                break
    return booked


def is_autopay_completion(comp: Completion) -> bool:
    return comp.performed_by_id is None and comp.notes == AUTOPAY_NOTE


def complete_many(db: Session, tasks: list[Task], *, user: User, done_on: date) -> int:
    """'Paid all' for a group of payments: each one with its planned amount."""
    for task in tasks:
        complete_task(db, task, user=user, done_on=done_on, amount_gr=task.amount_gr, km=None, vendor=None, notes=None)
    return len(tasks)


# ---------------------------------------------------------------- payment groups (dashboard)

PAYMENT_GROUP_MIN = 3  # fewer payments on one day stay as normal rows


@dataclass
class PaymentGroup:
    """Several payments due the same day, shown as one collapsible row."""

    date: date
    rows: list[TaskRow]

    @property
    def due(self) -> Due:
        return self.rows[0].due

    @property
    def total_gr(self) -> int:
        return sum(
            (r.task.amount_gr or 0) * (1 if r.task.direction == "income" else -1)
            for r in self.rows
            if r.task.direction != "none"
        )

    @property
    def manual(self) -> list[TaskRow]:
        return [r for r in self.rows if not r.task.autopay]

    @property
    def auto_count(self) -> int:
        return len(self.rows) - len(self.manual)

    @property
    def assignees(self) -> list[User]:
        """Distinct people the payments of this day are assigned to (shown as initials)."""
        out: dict[int, User] = {}
        for r in self.rows:
            if r.task.assignee is not None:
                out.setdefault(r.task.assignee.id, r.task.assignee)
        return list(out.values())


def group_payments(rows: list[TaskRow]) -> list[TaskRow | PaymentGroup]:
    """Collapse payments due on the same day (rows sorted by date) into PaymentGroups."""
    by_day: dict[date, list[TaskRow]] = defaultdict(list)
    for r in rows:
        if r.task.category == "payment" and r.due.date and not r.due.by_km:
            by_day[r.due.date].append(r)
    grouped = {d for d, rs in by_day.items() if len(rs) >= PAYMENT_GROUP_MIN}
    out: list[TaskRow | PaymentGroup] = []
    seen: set[date] = set()
    for r in rows:
        d = r.due.date
        if d in grouped and r.task.category == "payment" and not r.due.by_km:
            if d not in seen:
                seen.add(d)
                out.append(PaymentGroup(d, by_day[d]))
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- who takes care of what


def active_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id)))


def any_assigned(db: Session) -> bool:
    """Is at least one active task assigned to someone? (otherwise 'Moje' == 'Wszyscy')"""
    stmt = select(func.count(Task.id)).where(Task.active.is_(True), Task.assignee_id.is_not(None))
    return (db.scalar(stmt) or 0) > 0


def rows_assigned_to(db: Session, tree: Tree, today: date, user_id: int) -> list[TaskRow]:
    """Active tasks of the whole household assigned to one account, by due date."""
    return [r for r in task_rows(db, tree, today) if r.task.assignee_id == user_id]


@dataclass
class ZoneRows:
    zone: Item
    rows: list[TaskRow]


def rows_by_zone(db: Session, tree: Tree, today: date) -> list[ZoneRows]:
    """All active tasks grouped by top-level zone (in tile order), each group by due date."""
    by_root: dict[int, list[TaskRow]] = defaultdict(list)
    for r in task_rows(db, tree, today):
        root = tree.root_of(r.task.item_id)
        if root is not None:
            by_root[root.id].append(r)
    return [ZoneRows(z, by_root[z.id]) for z in tree.roots() if by_root.get(z.id)]


def assign_tasks(db: Session, tasks: list[Task], assignee_id: int | None) -> int:
    """Set who takes care of the tasks (None = shared: everybody gets the reminders)."""
    if assignee_id is not None:
        u = db.get(User, assignee_id)
        if u is None or not u.is_active:
            raise ValueError("Nie ma takiego konta")
    for t in tasks:
        t.assignee_id = assignee_id
    db.commit()
    return len(tasks)


def undo_completion(db: Session, comp: Completion) -> None:
    """Revert the last completion: restore the previous due date."""
    task = comp.task
    if task.autopay and is_autopay_completion(comp):
        # otherwise the very next page view would book it again; the payment evidently did not go through
        task.autopay = False
    task.next_due_date = comp.due_date
    task.recurrence = recurrence.retreat(task.recurrence)  # gives the instalment back
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
            "category": task.category,
            "amount": task.amount_gr,
            "direction": task.direction,
            "group": gid,
        }
        if due.by_km and due.date and start <= due.date <= end:
            points.append(base | {"date": due.date.isoformat(), "kind": "forecast", "status": due.status})
        first = task.next_due_date
        dates = recurrence.occurrences(task.recurrence, first, min(start, first or start), end)
        for n, d in enumerate(dates):
            if d < start:
                continue
            if n >= MAX_POINTS_PER_TASK:
                break
            if n == 0 and d == task.next_due_date:
                status = compute_due(d, task.lead_days, today).status
            else:
                status = "future"
            # each occurrence of a counted series carries its own number ("rata 4/6")
            points.append(base | {"title": task.title_at(n), "date": d.isoformat(), "kind": "due", "status": status})

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
                "category": t.category,
                "amount": comp.actual_amount_gr,
                "direction": t.direction,
                "group": group_for(t.item_id),
                "date": comp.done_on.isoformat(),
                "kind": "done",
                "status": "done",
            }
        )
    return {"points": points, "groups": sorted(groups.values(), key=lambda g: g["order"])}


TIMELINE_BINS = ("day", "week", "month")
URGENT_STATUSES = frozenset({"overdue", "today", "soon"})
# category → plural label for a bundle ("💳 Opłaty · 17")
CATEGORY_PLURALS = {
    "insurance": "Ubezpieczenia",
    "inspection": "Przeglądy",
    "service": "Serwisy",
    "diy": "Do zrobienia",
    "payment": "Opłaty",
    "income": "Przychody",
    "tax": "Podatki",
    "health": "Zdrowie",
    "event": "Wydarzenia",
    "occasion": "Okazje",
    "shopping": "Zakupy",
    "other": "Inne",
}


def plural_pl(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _bin_range(d: date, size: str) -> tuple[date, date]:
    if size == "week":
        start = d - timedelta(days=d.weekday())
        return start, start + timedelta(days=6)
    if size == "month":
        start = d.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt - timedelta(days=1)
    return d, d


def bundle_points(points: list[dict[str, Any]], size: str, by_group: bool = False) -> list[dict[str, Any]]:
    """Merge ordinary points falling into one day/week/month into a single labelled "bundle" point.

    Overdue / today / soon occurrences and km forecasts are never merged – they must stay visible
    with their own title. Done and planned points are bundled separately.
    """
    if size not in TIMELINE_BINS:
        raise ValueError(size)
    keep: list[dict[str, Any]] = []
    bins: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for p in points:
        if p["kind"] == "forecast" or p["status"] in URGENT_STATUSES:
            keep.append(p)
            continue
        start, end = _bin_range(date.fromisoformat(p["date"]), size)
        bins[(p["kind"], start, end, p["group"] if by_group else None)].append(p)
    for (kind, start, end, group), ps in bins.items():
        if len(ps) == 1:
            keep.append(ps[0])
            continue
        ps.sort(key=lambda p: (p["date"], p["title"]))
        cats = Counter(p.get("category") or "other" for p in ps)
        dates = Counter(p["date"] for p in ps)
        # place the bundle on its busiest day (e.g. the 10th, when most bills are due)
        anchor = min(dates, key=lambda d: (-dates[d], d))
        icons = Counter(p["icon"] for p in ps)
        n = len(ps)
        what = (
            plural_pl(n, "zadanie", "zadania", "zadań")
            if kind == "due"
            else plural_pl(n, "wykonane", "wykonane", "wykonanych")
        )
        if len(cats) == 1:
            cat = next(iter(cats))
            label = CATEGORY_PLURALS.get(cat, "Inne")
        else:
            label = f"{n} {what}"
        summary = f"{n} {what}"
        if len(cats) > 1:
            summary += ": " + ", ".join(f"{CATEGORY_PLURALS.get(c, 'Inne')} {k}" for c, k in cats.most_common())
        statuses = {p["status"] for p in ps}
        status = "done" if kind == "done" else ("later" if "later" in statuses else "future")
        keep.append(
            {
                "kind": "bundle",
                "of": kind,
                "date": anchor,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "status": status,
                "count": n,
                "icons": [i for i, _ in icons.most_common(3)],
                "label": label,
                "category": next(iter(cats)) if len(cats) == 1 else None,
                "summary": summary,
                "group": group if by_group else ps[0]["group"],
                "tasks": [
                    {k: p.get(k) for k in ("task_id", "title", "zone", "icon", "date", "amount", "direction", "status")}
                    for p in ps
                ],
            }
        )
    keep.sort(key=lambda p: p["date"])
    return keep


def default_timeline_window(today: date) -> tuple[date, date]:
    return today - timedelta(days=14), today + timedelta(days=92)


# ---------------------------------------------------------------- month calendar

MONTH_NAMES = [
    "",
    "styczeń",
    "luty",
    "marzec",
    "kwiecień",
    "maj",
    "czerwiec",
    "lipiec",
    "sierpień",
    "wrzesień",
    "październik",
    "listopad",
    "grudzień",
]
CAL_WEEKDAYS = ("pon", "wt", "śr", "czw", "pt", "sob", "nd")
# the colour of a chip that stands for several tasks: the most pressing one wins
STATUS_RANK = {"overdue": 0, "today": 1, "soon": 2, "later": 3, "future": 4, "done": 5}
CAL_GROUP_MIN = 2  # this many tasks of one kind on one day become a single chip ("💳 Opłaty 17")


def calendar_points(
    db: Session, tree: Tree, today: date, start: date, end: date, *, user_id: int | None = None
) -> list[dict[str, Any]]:
    """Timeline points (due, km forecasts, done) of [start, end], each with its `assignee`.

    user_id given → only tasks of that person plus the shared (unassigned) ones, like "Moje" on the dashboard.
    """
    points = timeline(db, tree, today, start, end)["points"]
    ids = {p["task_id"] for p in points}
    assignees = {
        t.id: t.assignee for t in db.scalars(select(Task).where(Task.id.in_(ids)).options(selectinload(Task.assignee)))
    }
    out = []
    for p in points:
        who = assignees.get(p["task_id"])
        if user_id is not None and who is not None and who.id != user_id:
            continue
        out.append(p | {"assignee": who})
    return out


def group_day(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chips of one day: tasks of one category (and kind: due / done) merge into one bundle.

    Unlike the timeline bundles urgent ones merge too – a calendar cell has no room for 17 bills.
    Km forecasts always stay on their own.
    """
    bins: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    single: list[dict[str, Any]] = []
    for p in points:
        if p["kind"] == "forecast":
            single.append(p)
        else:
            bins[(p["kind"], p.get("category") or "other")].append(p)
    out = list(single)
    for (kind, cat), ps in bins.items():
        if len(ps) < CAL_GROUP_MIN:
            out.extend(ps)
            continue
        ps.sort(key=lambda p: (STATUS_RANK.get(p["status"], 9), p["title"]))
        n = len(ps)
        label = CATEGORY_PLURALS.get(cat, "Inne")
        what = (
            plural_pl(n, "zadanie", "zadania", "zadań")
            if kind == "due"
            else plural_pl(n, "wykonane", "wykonane", "wykonanych")
        )
        out.append(
            {
                "kind": "bundle",
                "of": kind,
                "date": ps[0]["date"],
                "status": ps[0]["status"],
                "count": n,
                "icon": ps[0]["icon"],
                "label": label,
                "category": cat,
                "summary": f"{n} {what}",
                "tasks": ps,
            }
        )
    out.sort(key=lambda p: (STATUS_RANK.get(p["status"], 9), p["kind"] != "bundle", p.get("title") or p["label"]))
    return out


@dataclass
class CalDay:
    date: date
    in_month: bool
    is_today: bool
    entries: list[dict[str, Any]]

    @property
    def weekend(self) -> bool:
        return self.date.weekday() >= 5

    @property
    def count(self) -> int:
        """Number of tasks on this day (a bundle counts with all its tasks)."""
        return sum(e["count"] if e["kind"] == "bundle" else 1 for e in self.entries)

    @property
    def dots(self) -> list[str]:
        """Phone cells: one coloured dot per distinct status, most pressing first."""
        sts = {("forecast" if e["kind"] == "forecast" else e["status"]) for e in self.entries}
        return sorted(sts, key=lambda s: STATUS_RANK.get(s, 3.5))


@dataclass
class CalMonth:
    year: int
    month: int
    weeks: list[list[CalDay]]

    @property
    def title(self) -> str:
        return f"{MONTH_NAMES[self.month]} {self.year}"

    @property
    def first(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def prev(self) -> date:
        return (self.first - timedelta(days=1)).replace(day=1)

    @property
    def next(self) -> date:
        return (self.first + timedelta(days=32)).replace(day=1)

    def days(self) -> list[CalDay]:
        return [d for w in self.weeks for d in w]

    def default_day(self, today: date) -> date:
        """Today, or the nearest later day with something due; another month: its first busy day."""
        busy = [d.date for d in self.days() if d.in_month and d.entries]
        if self.first <= today < self.next:
            if any(d.date == today and d.entries for d in self.days()):
                return today
            later = [d for d in busy if d >= today]
            return later[0] if later else today
        return busy[0] if busy else self.first


def month_grid(year: int, month: int) -> tuple[date, date]:
    """First Monday and last Sunday of the weeks covering the month."""
    first = date(year, month, 1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first - timedelta(days=first.weekday()), last + timedelta(days=6 - last.weekday())


def calendar_month(
    db: Session, tree: Tree, today: date, year: int, month: int, *, user_id: int | None = None
) -> CalMonth:
    start, end = month_grid(year, month)
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in calendar_points(db, tree, today, start, end, user_id=user_id):
        by_day[p["date"]].append(p)
    weeks: list[list[CalDay]] = []
    d = start
    while d <= end:
        week = []
        for _ in range(7):
            week.append(CalDay(d, d.month == month, d == today, group_day(by_day.get(d.isoformat(), []))))
            d += timedelta(days=1)
        weeks.append(week)
    return CalMonth(year, month, weeks)


def calendar_day(db: Session, tree: Tree, today: date, day: date, *, user_id: int | None = None) -> list[dict]:
    return group_day(calendar_points(db, tree, today, day, day, user_id=user_id))
