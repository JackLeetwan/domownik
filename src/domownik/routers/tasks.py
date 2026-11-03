from __future__ import annotations

import calendar
from datetime import date, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import budget, documents, services
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
from domownik.web import is_htmx, render

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


# Simple "Powtarzanie" choices of the short form. With a date they become calendar rules
# anchored on that day (the 31st stays the last day of the month, no drift); without one
# (preview only) an equivalent interval counted from the due date.
PRESETS: dict[str, tuple[int, str]] = {
    "week": (1, "week"),
    "month": (1, "month"),
    "quarter": (3, "month"),
    "halfyear": (6, "month"),
    "year": (1, "year"),
}
FREQ_LABELS = {
    "once": "Jednorazowo",
    "week": "Co tydzień",
    "month": "Co miesiąc",
    "quarter": "Co kwartał",
    "halfyear": "Co pół roku",
    "year": "Co rok",
    "custom": "Inaczej…",
    "km": "Wg przebiegu…",
}


def preset_rule(freq: str, due: date | None) -> dict:
    every, unit = PRESETS[freq]
    if due is None or freq == "week":
        return {"mode": "interval", "every": every, "unit": unit, "anchor": "due"}
    if freq == "month":
        return {"mode": "monthly", "day": due.day}
    months = sorted({(due.month - 1 + k * every) % 12 + 1 for k in range(12 // every if unit == "month" else 1)})
    # MM-DD must exist in a leap year; shorter months are clamped when the date is computed
    return {"mode": "fixed", "dates": [f"{m:02d}-{min(due.day, calendar.monthrange(2024, m)[1]):02d}" for m in months]}


def preset_of(rule: dict, due: date | None = None) -> str:
    """Which simple choice shows this rule (anything else is edited under "Inaczej…")."""
    mode = rule.get("mode")
    if mode == "once":
        return "once"
    if rule.get("km"):
        return "km"
    core = {k: v for k, v in rule.items() if k not in ("count", "done", "until", "postponed_from")}
    for key, (every, unit) in PRESETS.items():
        if core == {"mode": "interval", "every": every, "unit": unit, "anchor": "due"}:
            return key
        if due is not None and core == recurrence.validate(preset_rule(key, due)):
            return key
    return "custom"


def end_of(rule: dict) -> str:
    return "count" if rule.get("count") else "until" if rule.get("until") else "never"


def _custom_raw(form) -> dict:
    return {
        "mode": form.get("mode") or "once",
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


def rule_from_form(form) -> dict:
    """Rule from the task form. Without ``freq`` (older clients) the full editor fields are used."""
    freq = form.get("freq")
    if freq == "once":
        return {"mode": "once"}
    if freq in PRESETS:
        due_s = (form.get("next_due_date") or "").strip()
        raw = preset_rule(freq, date.fromisoformat(due_s) if due_s else None)
    else:
        raw = _custom_raw(form)
        if freq == "km":
            raw["mode"] = "interval"
            if not raw["km"]:
                raise recurrence.RuleError("Podaj, co ile kilometrów")
    end = form.get("end") or "never"
    if end == "count":
        if not _int(form.get("count")):
            raise recurrence.RuleError("Podaj, ile razy (np. 6 rat)")
        raw["count"] = _int(form.get("count"))
    elif end == "until":
        if not (form.get("until") or "").strip():
            raise recurrence.RuleError("Podaj datę końca serii")
        raw["until"] = form.get("until").strip()
    return recurrence.validate(raw)


def _form_context(db: Session, tree: Tree, **extra) -> dict:
    task = extra.get("task")
    item = tree.items.get(task.item_id) if task is not None else None
    is_car = bool((item is not None and item.kind == "car") or (task is not None and (task.recurrence or {}).get("km")))
    return {
        "is_car": is_car,
        "categories": CATEGORIES,
        "directions": DIRECTIONS,
        "budget_categories": [
            c
            for c in db.scalars(select(BudgetCategory).order_by(BudgetCategory.sort_order, BudgetCategory.id))
            if not c.archived or (task is not None and c.id == task.budget_category_id)
        ],
        "users": list(db.scalars(select(User).where(User.is_active.is_(True)))),
        "items": sorted(tree.items.values(), key=lambda i: tree.label(i.id)),
        "label": tree.label,
        "FREQ_LABELS": FREQ_LABELS,
        "preset_of": preset_of,
        "end_of": end_of,
        "today": today(),
        **extra,
    }


def _apply_form(task: Task, form, db: Session) -> None:
    task.title = (form.get("title") or "").strip() or task.title or "Zadanie"
    task.item_id = int(form["item_id"])
    task.category = form.get("category") if form.get("category") in CATEGORIES else "other"
    old_rule = dict(task.recurrence or {})
    old_due = task.next_due_date
    rule = rule_from_form(form)
    due = (form.get("next_due_date") or "").strip()
    task.next_due_date = date.fromisoformat(due) if due else None
    if task.next_due_date is None:
        if "freq" in form and not rule.get("km") and not recurrence.is_calendar(rule):
            raise ValueError("Podaj, kiedy – datę najbliższego terminu")
        if rule["mode"] != "once":
            task.next_due_date = recurrence.first_due(rule, today())
    # state kept across edits: instalments already used up, an occurrence moved with "Przełóż"
    if rule.get("count") and old_rule.get("count"):
        rule["done"] = int(old_rule.get("done") or 0)
    if old_rule.get("postponed_from") and task.next_due_date == old_due and rule["mode"] != "once":
        rule["postponed_from"] = old_rule["postponed_from"]
    task.recurrence = recurrence.validate(rule)
    if rule.get("until") and task.next_due_date and task.next_due_date > date.fromisoformat(rule["until"]):
        raise ValueError("Koniec serii wypada przed najbliższym terminem")
    task.lead_days = _int(form.get("lead_days"), 7)
    task.next_due_km = _int(form.get("next_due_km"))
    amount = parse_pln(form.get("amount"))
    direction = form.get("direction")
    if direction not in DIRECTIONS:
        # the short form only offers expense / income; a task without money stays without
        direction = "expense" if amount else (task.direction if task.direction in DIRECTIONS else "expense")
    task.direction = direction
    task.amount_gr = amount if direction != "none" else None
    task.budget_category_id = _int(form.get("budget_category_id"))
    task.assignee_id = _int(form.get("assignee_id"))
    task.autopay = bool(form.get("autopay"))
    task.notes = (form.get("notes") or "").strip() or None
    # a counted series that is already used up stays finished until it gets more occurrences
    left = recurrence.remaining(task.recurrence)
    task.active = left is None or left > 0


# Pre-selected category of a new task, guessed from the kind of item it belongs to
DEFAULT_CATEGORY_BY_KIND = {"car": "service", "appliance": "service"}
DEFAULT_LEAD_DAYS = 7


def _is_payments_place(item) -> bool:
    """Mostly bills here (a "payments" zone)? Then a new task is a monthly payment by default."""
    tasks = [t for t in item.tasks if t.active]
    pay = sum(t.category in ("payment", "income") for t in tasks)
    return len(tasks) >= 2 and pay * 2 > len(tasks)


def _default_category(item, tree: Tree) -> str:
    if _is_payments_place(item):
        return "payment"
    return DEFAULT_CATEGORY_BY_KIND.get(item.kind, "other")


def _more_open(task: Task, default_category: str) -> bool:
    """Open "Więcej szczegółów" when something in it differs from what a new task would get."""
    return bool(
        task.category != default_category
        or task.lead_days != DEFAULT_LEAD_DAYS
        or task.budget_category_id
        or task.autopay
        or task.assignee_id
        or task.notes
        or task.next_due_km
    )


@router.get("/tasks/new")
def new_task_form(request: Request, item: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    tree = Tree.load(db)
    if item not in tree.items:
        raise HTTPException(404)
    it = tree.items[item]
    # bills: monthly is what almost every new entry in a payments zone is; anything else starts as a one-off
    rule = (
        {"mode": "interval", "every": 1, "unit": "month", "anchor": "due"}
        if _is_payments_place(it)
        else {"mode": "once"}
    )
    task = Task(
        item_id=item,
        category=_default_category(it, tree),
        recurrence=rule,
        lead_days=DEFAULT_LEAD_DAYS,
        direction="expense",
    )
    # a guess, so that a task with an amount lands in the right place of the budget
    task.budget_category_id = budget.suggest_category(db, tree, item, task.category)
    return render(
        request,
        "task_form.html",
        _form_context(db, tree, user=user, task=task, is_new=True, path=tree.path(item), more_open=False),
    )


@router.post("/tasks/new", dependencies=[Depends(verify_csrf)])
async def create_task(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    task = Task()
    try:
        _apply_form(task, form, db)
        # the reminder of a warranty / contract: a new date here is the new end date
        linked = documents.warranty_for_task(db, task.id)
        if linked is not None and task.next_due_date and task.recurrence.get("mode") == "once":
            linked.ends_on = task.next_due_date
    except (recurrence.RuleError, ValueError) as exc:
        return HTMLResponse(
            f'<p class="error">{exc}</p>',
            status_code=422,
            headers={"HX-Retarget": "#form-error", "HX-Reswap": "innerHTML"},
        )
    db.add(task)
    db.commit()
    return _redirect(f"/items/{task.item_id}")


PREVIEW_SHOWN = 4


@router.get("/tasks/preview")
def preview(request: Request, user: User = Depends(current_user)):
    """Live description of the rule under the form; dates only once "Kiedy" is filled in."""
    form = request.query_params
    dates: list[date] = []
    last = None
    try:
        rule = rule_from_form(form)
        start_s = (form.get("next_due_date") or "").strip()
        if start_s:
            start = date.fromisoformat(start_s)
            horizon = start + timedelta(days=366 * 10)
            all_dates = recurrence.occurrences(rule, start, start, horizon)
            dates = all_dates[:PREVIEW_SHOWN]
            if (rule.get("count") or rule.get("until")) and len(all_dates) > PREVIEW_SHOWN:
                last = all_dates[-1]
        text = recurrence.describe(rule)
        if form.get("freq") in PRESETS and not start_s:
            text = (
                FREQ_LABELS[form["freq"]].lower()
                + text[len(recurrence.describe({**rule, "count": None, "until": None})) :]
            )
        error = None
    except (recurrence.RuleError, ValueError) as exc:
        dates, text, error = [], "", str(exc)
    return render(
        request,
        "partials/preview.html",
        {"dates": dates, "last": last, "text": text, "error": error, "has_date": bool(form.get("next_due_date"))},
    )


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
    # a plain request (refresh, opened link, boosted navigation) gets a whole page, htmx the window content
    page = not is_htmx(request) or request.headers.get("hx-boosted") == "true"
    return render(
        request,
        "task_page.html" if page else "partials/task_modal.html",
        {
            "page": page,
            "path": tree.path(task.item_id) if page else [],
            "user": user,
            "task": task,
            "due": due,
            "zone": tree.label(task.item_id),
            "upcoming": upcoming,
            "last_km": services.last_km(readings.get(task.item_id, [])),
            "is_car": task.item.kind == "car" or bool(task.recurrence.get("km")),
            "today": today(),
            "users": services.active_users(db),
            "postpone_to": _postpone_choices(task.next_due_date, today()),
            "left": recurrence.remaining(task.recurrence),
        },
    )


def _on_task_page(request: Request, task_id: int) -> bool:
    """Was the action sent from the full task page (/tasks/{id}) rather than the window over a list?"""
    current = request.headers.get("hx-current-url") or ""
    return urlsplit(current).path.rstrip("/") == f"/tasks/{task_id}"


def _after_action(request: Request, task: Task, *, leave: bool) -> HTMLResponse:
    """Reload the page the window was opened over; the full task page goes back to its zone after
    "Zrobione"/"Pomiń" (and whenever the task has nothing left to show)."""
    if _on_task_page(request, task.id) and (leave or not task.active):
        return HTMLResponse("", headers={"HX-Redirect": f"/items/{task.item_id}"})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


def _postpone_choices(due: date | None, t: date) -> list[tuple[str, date]]:
    """Quick "Przełóż" buttons, counted from today or from the due date if it is still ahead."""
    if due is None:
        return []
    base = max(due, t)
    if base == t:
        return [("Jutro", t + timedelta(days=1)), ("Za tydzień", t + timedelta(days=7))]
    return [("O dzień", base + timedelta(days=1)), ("O tydzień", base + timedelta(days=7))]


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
    # invoice / receipt photographed with the completion: stored first, so a bad file changes nothing
    files = documents.uploads_in(form)
    attached = []
    if files:
        try:
            attached = documents.store_files(
                db, task.item, [(f.filename or "", f.file) for f in files], user=user, doc_kind="invoice"
            )
        except documents.UploadError as exc:
            db.rollback()
            return HTMLResponse(
                f'<p class="error">{escape(str(exc))}</p>',
                status_code=422,
                headers={"HX-Retarget": "#done-error", "HX-Reswap": "innerHTML"},
            )
    comp = services.complete_task(
        db,
        task,
        user=user,
        done_on=done_on,
        amount_gr=amount,
        km=km,
        vendor=(form.get("vendor") or "").strip(),
        notes=(form.get("notes") or "").strip(),
    )
    if attached:
        for att in attached:
            att.completion_id = comp.id
        db.commit()
    return _after_action(request, task, leave=True)


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
def skip(request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Skip this occurrence without recording a completion.

    In a counted series the skipped occurrence is used up (a skipped instalment is still
    one of the six); to move it instead, use "Przełóż".
    """
    task = _get_task(db, task_id)
    task.recurrence, nxt = recurrence.advance(
        task.recurrence, previous_due=task.next_due_date, done_on=task.next_due_date or today()
    )
    task.next_due_date = nxt
    if nxt is None:
        task.active = False
    db.commit()
    return _after_action(request, task, leave=True)


@router.post("/tasks/{task_id}/postpone", dependencies=[Depends(verify_csrf)])
async def postpone(request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Move only the current occurrence to another day ("Przełóż"): no history entry, the cycle stays."""
    task = _get_task(db, task_id)
    form = await request.form()
    try:
        new = date.fromisoformat((form.get("to") or "").strip())
    except ValueError:
        return _postpone_error("Wybierz datę")
    if task.next_due_date is None or not task.active:
        return _postpone_error("To zadanie nie ma terminu do przełożenia")
    if new < today():
        return _postpone_error("Nie można przełożyć na dzień, który już minął")
    if new != task.next_due_date:
        rule = recurrence.postpone(task.recurrence, current_due=task.next_due_date)
        if rule.get("postponed_from") == new.isoformat():
            rule.pop("postponed_from")  # moved back to the original day
        task.recurrence = rule
        task.next_due_date = new
        db.commit()
    return _after_action(request, task, leave=False)


def _postpone_error(msg: str) -> HTMLResponse:
    return HTMLResponse(
        f'<p class="error">{msg}</p>',
        status_code=422,
        headers={"HX-Retarget": "#postpone-error", "HX-Reswap": "innerHTML"},
    )


@router.get("/tasks/{task_id}/edit")
def edit_task_form(request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    tree = Tree.load(db, include_archived=True)
    more_open = _more_open(task, _default_category(task.item, tree))
    return render(
        request,
        "task_form.html",
        _form_context(db, tree, user=user, task=task, is_new=False, path=tree.path(task.item_id), more_open=more_open),
    )


@router.post("/tasks/{task_id}/edit", dependencies=[Depends(verify_csrf)])
async def update_task(
    request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    task = _get_task(db, task_id)
    form = await request.form()
    try:
        _apply_form(task, form, db)
        # the reminder of a warranty / contract: a new date here is the new end date
        linked = documents.warranty_for_task(db, task.id)
        if linked is not None and task.next_due_date and task.recurrence.get("mode") == "once":
            linked.ends_on = task.next_due_date
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
