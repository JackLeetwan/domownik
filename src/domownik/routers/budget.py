"""/budzet – the household budget of a month, quick entries, categories with envelopes, monthly incomes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik import budget, goals, services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.money import parse_pln
from domownik.domain.tree import Tree
from domownik.models import BUDGET_KINDS, BudgetCategory, Item, RecurringIncome, Task, Transaction, User
from domownik.routers.calendar import _month
from domownik.security import current_user, verify_csrf
from domownik.web import error_target, is_quick, quick_saved, render

router = APIRouter()


def _mkey(d: date) -> str:
    return d.strftime("%Y-%m")


def _back(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _to_month(m: str | None, key: str | None = None) -> RedirectResponse:
    y, mo = _month(m)
    url = f"/budzet?m={y:04d}-{mo:02d}"
    if key:
        url += f"&open={key}#k{key}"
    return _back(url)


def _error(msg: str, target: str = "#bud-error") -> HTMLResponse:
    return HTMLResponse(
        f'<p class="error">{msg}</p>',
        status_code=422,
        headers={"HX-Retarget": target, "HX-Reswap": "innerHTML"},
    )


def _amount(raw) -> int:
    value = parse_pln(raw)
    if not value:
        raise ValueError("Podaj kwotę")
    if abs(value) > 100_000_000_00:
        raise ValueError("Za duża kwota")
    return abs(value)


def _category(db: Session, raw) -> BudgetCategory:
    try:
        cat = db.get(BudgetCategory, int(raw))
    except TypeError, ValueError:
        cat = None
    if cat is None:
        raise ValueError("Wybierz kategorię")
    return cat


def month_title(d: date) -> str:
    return f"{services.MONTH_NAMES[d.month]} {d.year}"


@router.get("/budzet")
def budget_page(
    request: Request,
    m: str | None = None,
    open: str | None = None,  # the expanded category after an action
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    t = today()
    services.settle_autopay(db, t)
    y, mo = _month(m)
    tree = Tree.load(db, include_archived=True)
    b = budget.compute_month(db, y, mo, t, tree)
    cats = budget.active_categories(db)
    suggestions = {
        task.id: budget.suggest_category(db, tree, task.item_id, task.category) for task in b.unassigned_tasks
    }
    live = Tree.load(db)
    default_day = t if b.is_current else b.first
    return render(
        request,
        "budget.html",
        {
            "user": user,
            "b": b,
            "title": month_title(b.first),
            "mkey": _mkey(b.first),
            "prev_key": _mkey(b.prev),
            "next_key": _mkey(b.next),
            "open": open,
            "categories": cats,
            "kinds": BUDGET_KINDS,
            "default_cat": budget.last_used_category(db, user.id) or _default_expense_category(cats),
            "default_day": default_day,
            "suggestions": suggestions,
            "items": sorted(live.items.values(), key=lambda i: live.label(i.id)),
            "label": live.label,
            "has_incomes": db.scalar(select(func.count(RecurringIncome.id))) > 0,
            "goals": goals.views(db),
            "goals_done": goals.views(db, done=True),
            "goal_today": t,
        },
    )


def _default_expense_category(cats: list[BudgetCategory]) -> int | None:
    for c in cats:
        if c.name == "Jedzenie":
            return c.id
    variable = [c for c in cats if c.kind == "variable"]
    return variable[0].id if variable else None


@router.post("/budzet/wpis", dependencies=[Depends(verify_csrf)])
async def add_entry(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Quick entry: an expense, or money in when an income category is chosen."""
    form = await request.form()
    try:
        amount = _amount(form.get("amount"))
        cat = _category(db, form.get("budget_category_id"))
        booked = date.fromisoformat((form.get("booked_on") or "").strip() or today().isoformat())
    except ValueError as exc:
        return _error(str(exc).replace("Invalid isoformat string", "Zła data"), error_target(request, "#bud-error"))
    item_id = None
    raw_item = str(form.get("item_id") or "").strip()
    if raw_item.isdigit() and db.get(Item, int(raw_item)) is not None:
        item_id = int(raw_item)
    db.add(
        Transaction(
            booked_on=booked,
            amount_gr=amount if cat.is_income else -amount,
            budget_category_id=cat.id,
            item_id=item_id,
            note=(str(form.get("note") or "").strip()[:300]) or None,
            created_by_id=user.id,
        )
    )
    db.commit()
    if is_quick(request):
        return quick_saved("Dodano wpływ" if cat.is_income else "Dodano wydatek")
    return _to_month(_mkey(booked), str(cat.id))


def _get_tx(db: Session, tx_id: int) -> Transaction:
    tx = db.get(Transaction, tx_id)
    if tx is None:
        raise HTTPException(404, "Nie ma takiego wpisu")
    return tx


def _tx_key(tx: Transaction) -> str:
    if tx.budget_category_id:
        return str(tx.budget_category_id)
    return "none-in" if tx.amount_gr > 0 else "none"


@router.post("/budzet/transakcje/{tx_id}/kwota", dependencies=[Depends(verify_csrf)])
async def edit_amount(request: Request, tx_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    tx = _get_tx(db, tx_id)
    form = await request.form()
    try:
        amount = _amount(form.get("amount"))
    except ValueError as exc:
        return _error(str(exc), f"#tx-error-{tx.id}")
    budget.set_transaction_amount(tx, amount)
    db.commit()
    return _to_month(form.get("m"), _tx_key(tx))


@router.post("/budzet/transakcje/{tx_id}/usun", dependencies=[Depends(verify_csrf)])
async def delete_tx(request: Request, tx_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Delete a manual entry or undo an income confirmation. Task payments are undone at the task."""
    tx = _get_tx(db, tx_id)
    if tx.completion_id is not None:
        raise HTTPException(400, "Płatność zadania cofnij w historii zadania")
    form = await request.form()
    key = _tx_key(tx)
    db.delete(tx)
    db.commit()
    return _to_month(form.get("m"), key)


@router.post("/budzet/dochody/{inc_id}/wplynelo", dependencies=[Depends(verify_csrf)])
async def income_received(
    request: Request, inc_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    inc = db.get(RecurringIncome, inc_id)
    if inc is None:
        raise HTTPException(404)
    form = await request.form()
    y, mo = _month(form.get("m"))
    try:
        amount = parse_pln(form.get("amount"))
    except ValueError as exc:
        return _error(str(exc), f"#inc-error-{inc.id}")
    tx = budget.confirm_income(db, inc, y, mo, amount_gr=amount, today=today(), user_id=user.id)
    db.commit()
    return _to_month(f"{y:04d}-{mo:02d}", _tx_key(tx))


@router.post("/budzet/zadania/{task_id}/kategoria", dependencies=[Depends(verify_csrf)])
async def task_category(
    request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404)
    form = await request.form()
    try:
        cat = _category(db, form.get("budget_category_id"))
    except ValueError as exc:
        return _error(str(exc), f"#task-error-{task.id}")
    budget.assign_task_category(db, task, cat.id)
    db.commit()
    return _to_month(form.get("m"), str(cat.id))


# ---------------------------------------------------------------- categories, envelopes and incomes


@router.get("/budzet/kategorie")
def categories_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    cats = list(db.scalars(select(BudgetCategory).order_by(BudgetCategory.sort_order, BudgetCategory.id)))
    usage = dict(
        db.execute(
            select(Task.budget_category_id, func.count(Task.id))
            .where(Task.active.is_(True), Task.budget_category_id.is_not(None))
            .group_by(Task.budget_category_id)
        ).all()
    )
    incomes = list(db.scalars(select(RecurringIncome).order_by(RecurringIncome.sort_order, RecurringIncome.id)))
    t = today()
    return render(
        request,
        "budget_categories.html",
        {
            "user": user,
            "by_kind": {k: [c for c in cats if c.kind == k] for k in BUDGET_KINDS},
            "kinds": BUDGET_KINDS,
            "usage": usage,
            "incomes": incomes,
            "income_categories": [c for c in cats if c.is_income and not c.archived],
            "zones": Tree.load(db).roots(),
            "goal_today": t,
        },
    )


def _apply_category(cat: BudgetCategory, form) -> None:
    name = str(form.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("Podaj nazwę kategorii")
    kind = form.get("kind")
    if kind not in BUDGET_KINDS:
        raise ValueError("Wybierz rodzaj")
    cat.name = name
    cat.kind = kind
    cat.icon = str(form.get("icon") or "").strip()[:16] or None
    limit = parse_pln(form.get("limit"))
    if limit is not None and limit < 0:
        raise ValueError("Limit nie może być ujemny")
    cat.monthly_limit_gr = limit or None


@router.post("/budzet/kategorie", dependencies=[Depends(verify_csrf)])
async def add_category(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    cat = BudgetCategory(sort_order=(db.scalar(select(func.max(BudgetCategory.sort_order))) or 0) + 1)
    try:
        _apply_category(cat, form)
    except ValueError as exc:
        return _error(str(exc), "#cat-error-new")
    if db.scalar(select(BudgetCategory).where(func.lower(BudgetCategory.name) == cat.name.lower())):
        return _error("Taka kategoria już jest", "#cat-error-new")
    db.add(cat)
    db.commit()
    return _back(f"/budzet/kategorie#c{cat.id}")


def _get_cat(db: Session, cat_id: int) -> BudgetCategory:
    cat = db.get(BudgetCategory, cat_id)
    if cat is None:
        raise HTTPException(404)
    return cat


@router.post("/budzet/kategorie/{cat_id}", dependencies=[Depends(verify_csrf)])
async def edit_category(
    request: Request, cat_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    cat = _get_cat(db, cat_id)
    form = await request.form()
    try:
        _apply_category(cat, form)
    except ValueError as exc:
        db.rollback()
        return _error(str(exc), f"#cat-error-{cat_id}")
    db.commit()
    back = form.get("back")
    if back == "budget":
        return _to_month(form.get("m"), str(cat.id))
    return _back(f"/budzet/kategorie#c{cat.id}")


@router.post("/budzet/kategorie/{cat_id}/archiwum", dependencies=[Depends(verify_csrf)])
def archive_category(cat_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    cat = _get_cat(db, cat_id)
    cat.archived = not cat.archived
    db.commit()
    return _back(f"/budzet/kategorie#c{cat.id}")


def _apply_income(inc: RecurringIncome, form, db: Session) -> None:
    name = str(form.get("name") or "").strip()[:120]
    if not name:
        raise ValueError("Podaj nazwę, np. Pensja")
    inc.name = name
    inc.amount_gr = _amount(form.get("amount"))
    try:
        day = int(str(form.get("day") or "10").strip())
    except ValueError as exc:
        raise ValueError("Dzień miesiąca to liczba 1–31") from exc
    if not 1 <= day <= 31:
        raise ValueError("Dzień miesiąca to liczba 1–31")
    inc.day = day
    raw = str(form.get("budget_category_id") or "").strip()
    cat = db.get(BudgetCategory, int(raw)) if raw.isdigit() else None
    inc.budget_category_id = cat.id if cat is not None and cat.is_income else None
    raw_item = str(form.get("item_id") or "").strip()
    item = db.get(Item, int(raw_item)) if raw_item.isdigit() else None
    inc.item_id = item.id if item is not None else None


@router.post("/budzet/dochody", dependencies=[Depends(verify_csrf)])
async def add_income(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    t = today()
    inc = RecurringIncome(starts_on=t.replace(day=1), sort_order=0)
    try:
        _apply_income(inc, form, db)
    except ValueError as exc:
        return _error(str(exc), "#inc-error-new")
    db.add(inc)
    db.commit()
    return _back("/budzet/kategorie#dochody")


def _get_income(db: Session, inc_id: int) -> RecurringIncome:
    inc = db.get(RecurringIncome, inc_id)
    if inc is None:
        raise HTTPException(404)
    return inc


@router.post("/budzet/dochody/{inc_id}", dependencies=[Depends(verify_csrf)])
async def edit_income(request: Request, inc_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    inc = _get_income(db, inc_id)
    form = await request.form()
    try:
        _apply_income(inc, form, db)
    except ValueError as exc:
        db.rollback()
        return _error(str(exc), f"#inc-error-{inc_id}")
    db.commit()
    return _back("/budzet/kategorie#dochody")


@router.post("/budzet/dochody/{inc_id}/zakoncz", dependencies=[Depends(verify_csrf)])
def end_income(inc_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    inc = _get_income(db, inc_id)
    budget.end_income(db, inc, today())
    db.commit()
    return _back("/budzet/kategorie#dochody")


@router.post("/budzet/dochody/{inc_id}/wznow", dependencies=[Depends(verify_csrf)])
def resume_income(inc_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    inc = _get_income(db, inc_id)
    inc.ends_on = None
    db.commit()
    return _back("/budzet/kategorie#dochody")


# ---------------------------------------------------------------- yearly costs of zones


@router.get("/budzet/strefy")
def zone_costs_page(
    request: Request, y: int | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    from domownik import zonecosts

    t = today()
    year = y if y and 2000 <= y <= t.year + 1 else t.year
    zones, rest = zonecosts.year_costs(db, year)
    return render(
        request,
        "budget_zones.html",
        {
            "user": user,
            "year": year,
            "zones": zones,
            "rest": rest,
            "total_expense": sum(z.expense_gr for z in zones) + rest.expense_gr,
            "is_current": year == t.year,
            "months_passed": t.month if year == t.year else 12,
            "can_next": year < t.year,
        },
    )
