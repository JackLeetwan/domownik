"""/budzet/import – a bank statement (CSV) into the budget: preview with proposals, then book what was chosen."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import bankimport, budget
from domownik.config import today
from domownik.db import get_db
from domownik.models import BankLine, BankRule, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/budzet/import")

MAX_FILE = 5 * 1024 * 1024


def _page(request: Request, db: Session, user: User, **ctx):
    rules = list(db.scalars(select(BankRule).order_by(BankRule.pattern)))
    last = db.scalar(select(BankLine.booked_on).order_by(BankLine.booked_on.desc()).limit(1))
    return render(
        request,
        "budget_import.html",
        {"user": user, "rules": rules, "last_line": last, "categories": budget.active_categories(db)} | ctx,
    )


@router.get("")
def import_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _page(request, db, user)


@router.post("", dependencies=[Depends(verify_csrf)])
async def preview(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile) and not hasattr(upload, "read"):
        return _page(request, db, user, error="Wybierz plik CSV z banku.")
    raw = await upload.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        return _page(request, db, user, error="Plik jest za duży (max 5 MB).")
    try:
        lines = bankimport.parse_statement(raw)
    except bankimport.StatementError as exc:
        return _page(request, db, user, error=str(exc))
    pv = bankimport.propose(db, lines, today())
    return _page(request, db, user, preview=pv, filename=upload.filename)


@router.post("/zapisz", dependencies=[Depends(verify_csrf)])
async def save(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    remember = form.get("remember") == "1"
    chosen: list[tuple[bankimport.Line, str, bool]] = []
    i = 0
    while f"ref_{i}" in form and i < bankimport.MAX_LINES:
        try:
            ln = bankimport.Line(
                booked_on=date.fromisoformat(str(form[f"d_{i}"])),
                amount_gr=int(str(form[f"a_{i}"])),
                description=str(form.get(f"t_{i}") or "")[:300],
                ref=str(form[f"ref_{i}"])[:40],
            )
        except KeyError, ValueError:
            raise HTTPException(400, "Uszkodzony formularz importu") from None
        choice = str(form.get(f"c_{i}") or "skip")
        proposed = str(form.get(f"p_{i}") or "")
        # remember a category picked by hand (or a guess from the bank's category that was kept)
        wants_rule = remember and choice.startswith("cat:") and (choice != proposed or form.get(f"r_{i}") != "1")
        chosen.append((ln, choice, wants_rule))
        i += 1
    if not chosen:
        return RedirectResponse("/budzet/import", status_code=303)
    res = bankimport.book(db, chosen, user=user, today=today())
    return _page(request, db, user, result=res)


@router.post("/reguly/{rule_id}/usun", dependencies=[Depends(verify_csrf)])
def delete_rule(rule_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rule = db.get(BankRule, rule_id)
    if rule is not None:
        db.delete(rule)
        db.commit()
    return RedirectResponse("/budzet/import#reguly", status_code=303)
