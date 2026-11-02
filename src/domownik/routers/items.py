from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import catalog, services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.money import parse_pln
from domownik.domain.tree import Tree
from domownik.models import ITEM_KINDS, Completion, Issue, Item, JournalEntry, Measurement, Task, Todo, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/items")
issues_router = APIRouter(prefix="/issues")
notes_router = APIRouter()

HEALTH_CATEGORIES = ("health", "event")


def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _get_item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Nie ma takiego elementu")
    return item


def _parse_date(value) -> date | None:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def _picks_from_form(form, tpl: catalog.ItemTemplate) -> list[services.TaskPick]:
    picks = []
    for i, t in enumerate(tpl.tasks):
        if not form.get(f"pick_{i}"):
            continue
        picks.append(
            services.TaskPick(
                index=i,
                title=(form.get(f"title_{i}") or t.title).strip(),
                amount_gr=parse_pln(form.get(f"amount_{i}")),
                due=_parse_date(form.get(f"due_{i}")),
            )
        )
    return picks


def _attributes_from_form(form, tpl: catalog.ItemTemplate | None, existing: dict | None = None) -> dict:
    attrs = dict(existing or {})
    keys = [a["key"] for a in tpl.attributes] if tpl else []
    keys += [k for k in attrs if k not in keys]
    for k in keys:
        if f"attr_{k}" in form:
            v = (form.get(f"attr_{k}") or "").strip()
            if v:
                attrs[k] = v
            else:
                attrs.pop(k, None)
    return attrs


def _template_for_kind(kind: str) -> catalog.ItemTemplate | None:
    for tpl in catalog.templates().values():
        if tpl.kind == kind and tpl.attributes:
            return tpl
    return None


@router.get("/new")
def choose_template(
    request: Request, parent: int | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    tree = Tree.load(db)
    return render(
        request,
        "item_choose.html",
        {
            "user": user,
            "parent": tree.items.get(parent) if parent else None,
            "path": tree.path(parent) if parent else [],
            "templates": catalog.templates(),
        },
    )


@router.get("/new/{key}")
def new_item_form(
    request: Request,
    key: str,
    parent: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    tpl = catalog.templates().get(key)
    if tpl is None:
        raise HTTPException(404)
    tree = Tree.load(db)
    return render(
        request,
        "item_form.html",
        {
            "user": user,
            "tpl": tpl,
            "item": None,
            "parent": tree.items.get(parent) if parent else None,
            "path": tree.path(parent) if parent else [],
            "today": today(),
        },
    )


@router.post("/new/{key}", dependencies=[Depends(verify_csrf)])
async def create_item(request: Request, key: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    tpl = catalog.templates().get(key)
    if tpl is None:
        raise HTTPException(404)
    form = await request.form()
    parent_id = int(form["parent_id"]) if form.get("parent_id") else None
    if parent_id:
        _get_item(db, parent_id)
    name = (form.get("name") or tpl.name).strip()
    item = Item(
        parent_id=parent_id,
        kind=tpl.kind,
        name=name,
        icon=(form.get("icon") or tpl.icon).strip() or None,
        attributes=_attributes_from_form(form, tpl),
        notes=(form.get("notes") or "").strip() or None,
        sort_order=int(form.get("sort_order") or 0),
    )
    db.add(item)
    db.flush()
    services.ensure_budget_categories(db)
    services.add_template_tasks(db, item, tpl, _picks_from_form(form, tpl), today())
    db.commit()
    return _redirect(f"/items/{item.id}")


@router.get("/{item_id}")
def item_page(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = today()
    tree = Tree.load(db, include_archived=True)
    item = tree.items.get(item_id) or _get_item(db, item_id)
    vis = Tree.load(db)
    kids = vis.kids(item_id)
    stats = services.zone_stats(db, vis, kids, t)
    own_rows = services.task_rows(db, vis, t, {item_id})
    sub_rows = [r for r in services.task_rows(db, vis, t, vis.subtree_ids(item_id)) if r.task.item_id != item_id]
    history = list(
        db.scalars(
            select(Completion)
            .join(Task)
            .where(Task.item_id.in_(vis.subtree_ids(item_id) or {item_id}))
            .order_by(Completion.done_on.desc(), Completion.id.desc())
            .limit(30)
        )
    )
    inactive = list(db.scalars(select(Task).where(Task.item_id == item_id, Task.active.is_(False))))
    # only the most recent completion of a task can be undone
    latest: dict[int, Completion] = {}
    for c in history:
        best = latest.get(c.task_id)
        if best is None or (c.done_on, c.id) > (best.done_on, best.id):
            latest[c.task_id] = c
    undoable = {c.id for c in latest.values()}
    health_rows: list = []
    if item.is_member:
        health_rows = [r for r in own_rows if r.task.category in HEALTH_CATEGORIES]
        own_rows = [r for r in own_rows if r.task.category not in HEALTH_CATEGORIES]
    issues = services.subtree_issues(db, vis, item_id)
    km = services.km_readings(db, {item_id}).get(item_id, [])
    tpl = _template_for_kind(item.kind)
    return render(
        request,
        "item.html",
        {
            "user": user,
            "item": item,
            "path": tree.path(item_id),
            "kids": kids,
            "stats": stats,
            "own_rows": own_rows,
            "sub_rows": sub_rows,
            "history": history,
            "inactive": inactive,
            "undoable": undoable,
            "last_km": services.last_km(km),
            "attr_labels": {a["key"]: a["label"] for a in tpl.attributes} if tpl else {},
            "templates": catalog.templates(),
            "suggested": catalog.suggestions(item.kind, item.name, item.attributes or {}),
            "is_container": item.kind in ("group", "house", "apartment", "rental", "child"),
            "health_rows": health_rows,
            "open_issues": [i for i in issues if i.resolved_on is None],
            "closed_issues": [i for i in issues if i.resolved_on is not None],
            "todos": services.open_todos(db, vis, vis.subtree_ids(item_id)),
            "done_todos": sorted((t for t in item.todos if t.done_on), key=lambda t: (t.done_on, t.id), reverse=True)[
                :20
            ],
            "users": list(db.scalars(select(User).where(User.is_active.is_(True)))),
            "label": vis.label,
            "timeline_item": item_id,
        },
    )


@router.get("/{item_id}/edit")
def edit_item_form(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _get_item(db, item_id)
    tree = Tree.load(db, include_archived=True)
    # candidates for parent: everything outside the item's own subtree
    sub = tree.subtree_ids(item_id)
    parents = [i for i in tree.items.values() if i.id not in sub and not i.archived]
    return render(
        request,
        "item_edit.html",
        {
            "user": user,
            "item": item,
            "tpl": _template_for_kind(item.kind),
            "path": tree.path(item_id),
            "parents": sorted(parents, key=lambda i: tree.label(i.id)),
            "label": tree.label,
            "kinds": ITEM_KINDS,
        },
    )


@router.post("/{item_id}/edit", dependencies=[Depends(verify_csrf)])
async def update_item(
    request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    item = _get_item(db, item_id)
    form = await request.form()
    item.name = (form.get("name") or item.name).strip()
    item.icon = (form.get("icon") or "").strip() or None
    if form.get("kind") in ITEM_KINDS:
        item.kind = form["kind"]
    parent_id = int(form["parent_id"]) if form.get("parent_id") else None
    if parent_id is not None:
        tree = Tree.load(db, include_archived=True)
        if parent_id in tree.subtree_ids(item_id):
            raise HTTPException(400, "Nie można przenieść elementu do jego własnego podelementu")
    item.parent_id = parent_id
    item.sort_order = int(form.get("sort_order") or 0)
    item.notes = (form.get("notes") or "").strip() or None
    item.attributes = _attributes_from_form(form, _template_for_kind(item.kind), item.attributes)
    # free-form extra attribute
    new_key = (form.get("new_attr_key") or "").strip()
    new_val = (form.get("new_attr_value") or "").strip()
    if new_key and new_val:
        item.attributes = {**item.attributes, new_key: new_val}
    db.commit()
    return _redirect(f"/items/{item.id}")


@router.post("/{item_id}/archive", dependencies=[Depends(verify_csrf)])
def archive_item(item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _get_item(db, item_id)
    item.archived = not item.archived
    db.commit()
    if not item.archived:
        return _redirect(f"/items/{item.id}")
    return _redirect(f"/items/{item.parent_id}" if item.parent_id else "/")


@router.get("/{item_id}/template/{key}")
def template_tasks_form(
    request: Request, item_id: int, key: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    item = _get_item(db, item_id)
    tpl = catalog.templates().get(key)
    if tpl is None:
        raise HTTPException(404)
    existing = {t.title for t in item.tasks if t.active}
    return render(
        request,
        "template_tasks.html",
        {
            "user": user,
            "item": item,
            "tpl": tpl,
            "existing": existing,
            "path": Tree.load(db).path(item_id),
            "today": today(),
        },
    )


@router.post("/{item_id}/template/{key}", dependencies=[Depends(verify_csrf)])
async def add_template_tasks(
    request: Request, item_id: int, key: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    item = _get_item(db, item_id)
    tpl = catalog.templates().get(key)
    if tpl is None:
        raise HTTPException(404)
    form = await request.form()
    services.ensure_budget_categories(db)
    services.add_template_tasks(db, item, tpl, _picks_from_form(form, tpl), today())
    db.commit()
    return _redirect(f"/items/{item.id}")


@router.post("/{item_id}/km", dependencies=[Depends(verify_csrf)])
async def add_km(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _get_item(db, item_id)
    form = await request.form()
    km = int(str(form.get("km") or "0").replace(" ", "") or 0)
    if km > 0:
        db.add(
            Measurement(item_id=item_id, kind="odometer", measured_on=_parse_date(form.get("on")) or today(), value=km)
        )
        db.commit()
    return _redirect(f"/items/{item_id}")


# ---------------------------------------------------------------- household members: visits and issues


def _get_member(db: Session, item_id: int) -> Item:
    item = _get_item(db, item_id)
    if not item.is_member:
        raise HTTPException(400, "Wizyty i sprawy można dodawać tylko domownikom")
    return item


@router.post("/{item_id}/visit", dependencies=[Depends(verify_csrf)])
async def add_visit(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Quick one-off appointment: doctor, test, vaccination…"""
    item = _get_member(db, item_id)
    form = await request.form()
    title = (form.get("title") or "").strip()
    on = _parse_date(form.get("on"))
    if not title or on is None:
        raise HTTPException(400, "Podaj, co i kiedy")
    category = form.get("category") if form.get("category") in HEALTH_CATEGORIES else "health"
    db.add(
        Task(
            item_id=item.id,
            title=title,
            category=category,
            recurrence={"mode": "once", "date": on.isoformat()},
            lead_days=int(form.get("lead_days") or 2),
            next_due_date=on,
            direction="none",
            notes=(form.get("notes") or "").strip() or None,
        )
    )
    db.commit()
    return _redirect(f"/items/{item.id}")


@router.post("/{item_id}/issues", dependencies=[Depends(verify_csrf)])
async def add_issue(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _get_item(db, item_id)
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Podaj, co się dzieje")
    db.add(
        Issue(
            item_id=item.id,
            title=title,
            started_on=_parse_date(form.get("started_on")) or today(),
            notes=(form.get("notes") or "").strip() or None,
            created_by_id=user.id,
        )
    )
    db.commit()
    return _redirect(f"/items/{item.id}#sprawy")


def _get_issue(db: Session, issue_id: int) -> Issue:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(404, "Nie ma takiej sprawy")
    return issue


@issues_router.post("/{issue_id}/edit", dependencies=[Depends(verify_csrf)])
async def edit_issue(
    request: Request, issue_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    issue = _get_issue(db, issue_id)
    form = await request.form()
    issue.title = (form.get("title") or issue.title).strip()
    issue.started_on = _parse_date(form.get("started_on")) or issue.started_on
    issue.notes = (form.get("notes") or "").strip() or None
    if "resolved_on" in form:
        issue.resolved_on = _parse_date(form.get("resolved_on"))
    db.commit()
    return _back(request, f"/items/{issue.item_id}#sprawy")


@issues_router.post("/{issue_id}/toggle", dependencies=[Depends(verify_csrf)])
def toggle_issue(request: Request, issue_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    issue = _get_issue(db, issue_id)
    issue.resolved_on = None if issue.resolved_on else today()
    db.commit()
    return _back(request, f"/items/{issue.item_id}#sprawy")


@issues_router.post("/{issue_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_issue(request: Request, issue_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    issue = _get_issue(db, issue_id)
    item_id = issue.item_id
    db.delete(issue)
    db.commit()
    return _back(request, f"/items/{item_id}#sprawy")


# ---------------------------------------------------------------- notes: pinned info, todos, journal


def _int_or_none(v) -> int | None:
    v = str(v or "").strip()
    return int(v) if v else None


@router.post("/{item_id}/notes", dependencies=[Depends(verify_csrf)])
async def save_pinned(
    request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    item = _get_item(db, item_id)
    form = await request.form()
    item.notes = (form.get("notes") or "").strip() or None
    db.commit()
    return _redirect(f"/items/{item.id}")


@router.post("/{item_id}/todos", dependencies=[Depends(verify_csrf)])
async def add_todo(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _get_item(db, item_id)
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Wpisz, co jest do zrobienia")
    db.add(
        Todo(
            item_id=item.id,
            title=title,
            notes=(form.get("notes") or "").strip() or None,
            est_amount_gr=parse_pln(form.get("amount")),
            assignee_id=_int_or_none(form.get("assignee_id")),
            created_by_id=user.id,
        )
    )
    db.commit()
    return _redirect(f"/items/{item.id}#do-zrobienia")


def _get_todo(db: Session, todo_id: int) -> Todo:
    todo = db.get(Todo, todo_id)
    if todo is None:
        raise HTTPException(404, "Nie ma takiej pozycji")
    return todo


def _todo_home(item_id: int | None) -> str:
    return f"/items/{item_id}#do-zrobienia" if item_id else "/do-zrobienia"


def _back(request: Request, fallback: str) -> RedirectResponse:
    """Return to the page the action came from (dashboard or item page)."""
    nxt = request.query_params.get("next") or ""
    return _redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else fallback)


@notes_router.post("/todos/{todo_id}/toggle", dependencies=[Depends(verify_csrf)])
def toggle_todo(request: Request, todo_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    todo = _get_todo(db, todo_id)
    if todo.done_on:
        todo.done_on, todo.done_by_id = None, None
    else:
        todo.done_on, todo.done_by_id = today(), user.id
    db.commit()
    return _back(request, _todo_home(todo.item_id))


@notes_router.post("/todos/{todo_id}/edit", dependencies=[Depends(verify_csrf)])
async def edit_todo(request: Request, todo_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    todo = _get_todo(db, todo_id)
    form = await request.form()
    todo.title = (form.get("title") or todo.title).strip()
    todo.notes = (form.get("notes") or "").strip() or None
    todo.est_amount_gr = parse_pln(form.get("amount"))
    todo.assignee_id = _int_or_none(form.get("assignee_id"))
    if "item_id" in form:  # moving between items / to 'general'
        new_item = _int_or_none(form.get("item_id"))
        if new_item is not None:
            _get_item(db, new_item)
        todo.item_id = new_item
    db.commit()
    return _back(request, _todo_home(todo.item_id))


@notes_router.post("/todos/{todo_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_todo(request: Request, todo_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    todo = _get_todo(db, todo_id)
    item_id = todo.item_id
    db.delete(todo)
    db.commit()
    return _back(request, _todo_home(item_id))


@notes_router.post("/todos/{todo_id}/plan", dependencies=[Depends(verify_csrf)])
async def plan_todo(request: Request, todo_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Give a todo a date: it becomes a one-off task (timeline, reminders, budget) and leaves the list."""
    todo = _get_todo(db, todo_id)
    form = await request.form()
    on = _parse_date(form.get("on"))
    if on is None:
        raise HTTPException(400, "Podaj datę")
    target = todo.item_id or _int_or_none(form.get("item_id"))
    if target is None:
        raise HTTPException(400, "Wybierz, czego dotyczy zadanie (auto, dom, osoba…)")
    _get_item(db, target)
    db.add(
        Task(
            item_id=target,
            title=todo.title,
            category="diy",
            recurrence={"mode": "once", "date": on.isoformat()},
            lead_days=3,
            next_due_date=on,
            amount_gr=todo.est_amount_gr,
            direction="expense" if todo.est_amount_gr else "none",
            assignee_id=todo.assignee_id,
            notes=todo.notes,
        )
    )
    db.delete(todo)
    db.commit()
    return _back(request, f"/items/{target}")


@router.post("/{item_id}/journal", dependencies=[Depends(verify_csrf)])
async def add_journal(
    request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    item = _get_item(db, item_id)
    form = await request.form()
    body = (form.get("body") or "").strip()
    if not body:
        raise HTTPException(400, "Wpis jest pusty")
    db.add(
        JournalEntry(item_id=item.id, noted_on=_parse_date(form.get("on")) or today(), body=body, created_by_id=user.id)
    )
    db.commit()
    return _redirect(f"/items/{item.id}#dziennik")


def _get_entry(db: Session, entry_id: int) -> JournalEntry:
    entry = db.get(JournalEntry, entry_id)
    if entry is None:
        raise HTTPException(404, "Nie ma takiego wpisu")
    return entry


@notes_router.post("/journal/{entry_id}/edit", dependencies=[Depends(verify_csrf)])
async def edit_journal(
    request: Request, entry_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    entry = _get_entry(db, entry_id)
    form = await request.form()
    entry.body = (form.get("body") or entry.body).strip()
    entry.noted_on = _parse_date(form.get("on")) or entry.noted_on
    db.commit()
    return _redirect(f"/items/{entry.item_id}#dziennik")


@notes_router.post("/journal/{entry_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_journal(entry_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    entry = _get_entry(db, entry_id)
    item_id = entry.item_id
    db.delete(entry)
    db.commit()
    return _redirect(f"/items/{item_id}#dziennik")
