from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import services
from domownik.config import today
from domownik.db import get_db
from domownik.domain import recurrence
from domownik.domain.money import parse_pln
from domownik.domain.tree import Tree
from domownik.models import (
    CATEGORIES,
    DIRECTIONS,
    BudgetCategory,
    Completion,
    Task,
    User,
)
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _get_task(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Nie ma takiego zadania")
    return task


def _int(v, default=None):
    v = str(v or "").strip().replace(" ", "")
    return int(v) if v else default


def rule_from_form(form) -> dict:
    mode = form.get("mode") or "once"
    raw = {
        "mode": mode,
        "every": _int(form.get("every"), 1),
        "unit": form.get("unit") or "month",
        "anchor": form.get("anchor") or "completion",
        "km": _int(form.get("km")),
        "day": _int(form.get("day"), 1),
        "dates": form.get("dates") or "",
        "offset": _int(form.get("offset"), 0),
        "month": _int(form.get("month"), 1),
        "weekday": form.get("weekday") or "sun",
        "n": _int(form.get("n"), 1),
    }
    return recurrence.validate(raw)


def _form_context(db: Session, tree: Tree, **extra) -> dict:
    return {
        "categories": CATEGORIES,
        "directions": DIRECTIONS,
        "budget_categories": list(db.scalars(select(BudgetCategory).order_by(BudgetCategory.sort_order))),
        "users": list(db.scalars(select(User).where(User.is_active.is_(True)))),
        "items": sorted(tree.items.values(), key=lambda i: tree.label(i.id)),
        "label": tree.label,
        **extra,
    }


def _apply_form(task: Task, form, db: Session) -> None:
    task.title = (form.get("title") or "").strip() or task.title or "Zadanie"
    task.item_id = int(form["item_id"])
    task.category = form.get("category") if form.get("category") in CATEGORIES else "other"
    task.recurrence = rule_from_form(form)
    task.lead_days = _int(form.get("lead_days"), 7)
    due = (form.get("next_due_date") or "").strip()
    task.next_due_date = date.fromisoformat(due) if due else None
    if task.next_due_date is None and task.recurrence["mode"] != "once":
        task.next_due_date = recurrence.first_due(task.recurrence, today())
    task.next_due_km = _int(form.get("next_due_km"))
    task.direction = form.get("direction") if form.get("direction") in DIRECTIONS else "expense"
    task.amount_gr = parse_pln(form.get("amount")) if task.direction != "none" else None
    task.budget_category_id = _int(form.get("budget_category_id"))
    task.assignee_id = _int(form.get("assignee_id"))
    task.autopay = bool(form.get("autopay"))
    task.notes = (form.get("notes") or "").strip() or None
    task.active = True


# Pre-selected category of a new task, guessed from the kind of item it belongs to
DEFAULT_CATEGORY_BY_KIND = {"car": "service", "appliance": "service"}


@router.get("/tasks/new")
def new_task_form(request: Request, item: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    tree = Tree.load(db)
    if item not in tree.items:
        raise HTTPException(404)
    task = Task(
        item_id=item,
        category=DEFAULT_CATEGORY_BY_KIND.get(tree.items[item].kind, "other"),
        recurrence={"mode": "interval", "every": 1, "unit": "month", "anchor": "completion"},
        lead_days=7,
        direction="expense",
    )
    return render(
        request, "task_form.html", _form_context(db, tree, user=user, task=task, is_new=True, path=tree.path(item))
    )


@router.post("/tasks/new", dependencies=[Depends(verify_csrf)])
async def create_task(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    task = Task()
    try:
        _apply_form(task, form, db)
    except (recurrence.RuleError, ValueError) as exc:
        return HTMLResponse(
            f'<p class="error">{exc}</p>',
            status_code=422,
            headers={"HX-Retarget": "#form-error", "HX-Reswap": "innerHTML"},
        )
    db.add(task)
    db.commit()
    return _redirect(f"/items/{task.item_id}")


@router.get("/tasks/preview")
def preview(request: Request, user: User = Depends(current_user)):
    form = request.query_params
    try:
        rule = rule_from_form(form)
        start_s = (form.get("next_due_date") or "").strip()
        start = date.fromisoformat(start_s) if start_s else recurrence.first_due(rule, today())
        dates = recurrence.occurrences(rule, start, start or today(), (start or today()) + timedelta(days=366 * 5))[:4]
        text = recurrence.describe(rule)
        error = None
    except (recurrence.RuleError, ValueError) as exc:
        dates, text, error = [], "", str(exc)
    return render(request, "partials/preview.html", {"dates": dates, "text": text, "error": error})


@router.get("/tasks/{task_id}")
def task_modal(request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    tree = Tree.load(db, include_archived=True)
    readings = services.km_readings(db, {task.item_id})
    due = services.task_due(task, today(), readings)
    upcoming = recurrence.occurrences(
        task.recurrence,
        task.next_due_date,
        task.next_due_date or today(),
        (task.next_due_date or today()) + timedelta(days=366 * 3),
    )[1:4]
    return render(
        request,
        "partials/task_modal.html",
        {
            "user": user,
            "task": task,
            "due": due,
            "zone": tree.label(task.item_id),
            "upcoming": upcoming,
            "last_km": services.last_km(readings.get(task.item_id, [])),
            "is_car": task.item.kind == "car" or bool(task.recurrence.get("km")),
            "today": today(),
        },
    )


@router.post("/tasks/{task_id}/done", dependencies=[Depends(verify_csrf)])
async def complete(request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    form = await request.form()
    try:
        done_on = date.fromisoformat(form.get("done_on") or today().isoformat())
        amount = parse_pln(form.get("amount"))
        km = _int(form.get("km"))
    except ValueError as exc:
        return HTMLResponse(
            f'<p class="error">{exc}</p>',
            status_code=422,
            headers={"HX-Retarget": "#done-error", "HX-Reswap": "innerHTML"},
        )
    services.complete_task(
        db,
        task,
        user=user,
        done_on=done_on,
        amount_gr=amount,
        km=km,
        vendor=(form.get("vendor") or "").strip(),
        notes=(form.get("notes") or "").strip(),
    )
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/tasks/bulk-done", dependencies=[Depends(verify_csrf)])
async def bulk_done(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """'Paid all' for a group of payments on the dashboard."""
    form = await request.form()
    ids = {int(v) for v in form.getlist("task_id") if str(v).isdigit()}
    tasks = [t for t in db.scalars(select(Task).where(Task.id.in_(ids), Task.active.is_(True)))] if ids else []
    services.complete_many(db, tasks, user=user, done_on=today())
    return _redirect("/")


@router.post("/completions/{comp_id}/undo", dependencies=[Depends(verify_csrf)])
def undo(comp_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    comp = db.get(Completion, comp_id)
    if comp is None:
        raise HTTPException(404)
    item_id = comp.task.item_id
    services.undo_completion(db, comp)
    return _redirect(f"/items/{item_id}")


@router.post("/tasks/{task_id}/skip", dependencies=[Depends(verify_csrf)])
def skip(task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Skip this occurrence without recording a completion."""
    task = _get_task(db, task_id)
    nxt = recurrence.next_due(task.recurrence, previous_due=task.next_due_date, done_on=task.next_due_date or today())
    task.next_due_date = nxt
    if nxt is None:
        task.active = False
    db.commit()
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/tasks/{task_id}/edit")
def edit_task_form(request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    tree = Tree.load(db, include_archived=True)
    return render(
        request,
        "task_form.html",
        _form_context(db, tree, user=user, task=task, is_new=False, path=tree.path(task.item_id)),
    )


@router.post("/tasks/{task_id}/edit", dependencies=[Depends(verify_csrf)])
async def update_task(
    request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    task = _get_task(db, task_id)
    form = await request.form()
    try:
        _apply_form(task, form, db)
    except (recurrence.RuleError, ValueError) as exc:
        db.rollback()
        return HTMLResponse(
            f'<p class="error">{exc}</p>',
            status_code=422,
            headers={"HX-Retarget": "#form-error", "HX-Reswap": "innerHTML"},
        )
    db.commit()
    return _redirect(f"/items/{task.item_id}")


@router.post("/tasks/{task_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_task(task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    item_id = task.item_id
    db.delete(task)
    db.commit()
    return _redirect(f"/items/{item_id}")
