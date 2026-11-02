"""Przypisania: who takes care of which task (decides who gets the push reminders)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import Task, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/przypisania")

ONLY_FREE = "nieprzypisane"


def _assignee_from_form(form) -> int | None:
    raw = str(form.get("assignee_id") or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise HTTPException(400, "Nieprawidłowa osoba")
    return int(raw)


def _assign(db: Session, tasks: list[Task], assignee_id: int | None) -> None:
    try:
        services.assign_tasks(db, tasks, assignee_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _unassigned_count(db: Session, tree: Tree) -> int:
    return sum(1 for r in services.task_rows(db, tree, today()) if r.task.assignee_id is None)


@router.get("")
def assignments_page(
    request: Request,
    tylko: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    tree = Tree.load(db)
    groups = services.rows_by_zone(db, tree, today())
    total = sum(len(g.rows) for g in groups)
    unassigned = sum(1 for g in groups for r in g.rows if r.task.assignee_id is None)
    only_free = tylko == ONLY_FREE
    if only_free:
        groups = [services.ZoneRows(g.zone, [r for r in g.rows if r.task.assignee_id is None]) for g in groups]
        groups = [g for g in groups if g.rows]
    return render(
        request,
        "assignments.html",
        {
            "user": user,
            "groups": groups,
            "users": services.active_users(db),
            "total": total,
            "unassigned": unassigned,
            "only_free": only_free,
            "label": tree.label,
        },
    )


@router.post("/zbiorczo", dependencies=[Depends(verify_csrf)])
async def assign_many(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    ids = {int(v) for v in form.getlist("task_id") if str(v).isdigit()}
    tasks = list(db.scalars(select(Task).where(Task.id.in_(ids), Task.active.is_(True)))) if ids else []
    _assign(db, tasks, _assignee_from_form(form))
    url = "/przypisania" + (f"?tylko={ONLY_FREE}" if form.get("tylko") == ONLY_FREE else "")
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


@router.post("/{task_id}", dependencies=[Depends(verify_csrf)])
async def assign_one(
    request: Request,
    task_id: int,
    ctx: str = "list",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Quick change from the list (returns the row) or from the task window (returns the switch)."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Nie ma takiego zadania")
    form = await request.form()
    _assign(db, [task], _assignee_from_form(form))
    db.refresh(task)
    tree = Tree.load(db, include_archived=True)
    users = services.active_users(db)
    if ctx == "modal":
        return render(request, "partials/assign_control.html", {"task": task, "users": users, "oob": True})
    row = services.TaskRow(task, services.task_due(task, today(), services.km_readings(db, {task.item_id})), "")
    return render(
        request,
        "partials/assign_row.html",
        {
            "row": row,
            "users": users,
            "label": tree.label,
            "unassigned": _unassigned_count(db, Tree.load(db)),
            "oob": True,
        },
    )
