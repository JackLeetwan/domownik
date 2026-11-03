"""/zdjecia – 📥 Do rozpoznania: quick photos (see domownik.inbox)."""

from __future__ import annotations

from datetime import UTC, date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from domownik import budget, documents, inbox
from domownik.config import get_settings, today
from domownik.db import get_db
from domownik.domain.money import InvalidAmount, parse_pln
from domownik.domain.tree import Tree
from domownik.models import CAPTURE_KINDS, Attachment, User
from domownik.routers.quick import page_item_id
from domownik.security import current_user, verify_csrf
from domownik.web import is_htmx, quick_saved, render

router = APIRouter(prefix="/zdjecia")


def _att(db: Session, att_id: int) -> Attachment:
    att = db.get(Attachment, att_id)
    if att is None or not att.inbox or not documents.visible(db, att):
        raise HTTPException(404, "Tego zdjęcia już nie ma w „Do rozpoznania”")
    return att


def _error(msg: str) -> HTMLResponse:
    return HTMLResponse(msg, status_code=400, headers={"HX-Reswap": "none"})


def _taken_on(att: Attachment) -> date:
    """The day the photo was taken (created_at is UTC) – the default date of the service / receipt."""
    return att.created_at.replace(tzinfo=UTC).astimezone(get_settings().tz).date() if att.created_at else today()


def _ctx(db: Session, att: Attachment, mode: str, user: User) -> dict:
    tree = Tree.load(db)
    top, rest = inbox.item_choices(db, tree)
    if att.item_id and att.item_id in tree.items and all(i.id != att.item_id for i in top):
        top = [tree.items[att.item_id], *top[: inbox.QUICK_ITEMS - 1]]
        rest = [i for i in rest if i.id != att.item_id]
    tasks, best = inbox.task_choices(db, att.item_id, att.capture_kind)
    return {
        "user": user,
        "att": att,
        "mode": mode,
        "top_items": top,
        "rest_items": rest,
        "label": tree.label,
        "KINDS": CAPTURE_KINDS,
        "NEXT": inbox.NEXT,
        "default_next": inbox.DEFAULT_NEXT.get(att.capture_kind or "", "1y"),
        "tasks": tasks,
        "best_task": best,
        "taken_on": _taken_on(att),
        "categories": budget.active_categories(db, income=False),
        "suggested_category": budget.suggest_category(db, tree, att.item_id, "service") if att.item_id else None,
    }


def _card(request: Request, db: Session, att: Attachment, mode: str, user: User, **extra):
    name = "partials/capture_tag.html" if mode == "modal" else "partials/capture_card.html"
    return render(request, name, _ctx(db, att, mode, user) | extra)


@router.get("")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = inbox.pending(db)
    cards = [_ctx(db, a, "card", user) for a in rows]
    return render(request, "inbox.html", {"user": user, "cards": cards})


@router.post("", dependencies=[Depends(verify_csrf)])
async def upload(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    files = documents.uploads_in(form)
    tree = Tree.load(db)
    item_id = page_item_id(request, tree)
    try:
        created = documents.store_files(
            db, tree.items.get(item_id) if item_id else None, [(f.filename, f.file) for f in files], user=user
        )
    except documents.UploadError as exc:
        db.rollback()
        return _error(str(exc))
    for att in created:
        att.inbox = True
    db.commit()
    if form.get("from") == "page" or not is_htmx(request):
        resp = RedirectResponse("/zdjecia", status_code=303)
        resp.headers["HX-Redirect"] = "/zdjecia"
        return resp
    return _card(request, db, created[-1], "modal", user, added=len(created))


@router.post("/{att_id}/oznacz", dependencies=[Depends(verify_csrf)])
async def mark(request: Request, att_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    att = _att(db, att_id)
    form = await request.form()
    raw_item = str(form.get("item_id") or "")
    try:
        inbox.tag(
            db,
            att,
            item_id=int(raw_item) if raw_item.isdigit() else None,
            kind=str(form.get("kind")) if form.get("kind") else None,
        )
    except inbox.InboxError as exc:
        db.rollback()
        return _error(str(exc))
    db.commit()
    return _card(request, db, att, "modal" if form.get("mode") == "modal" else "card", user)


def _date(raw, fallback: date) -> date:
    raw = str(raw or "").strip()
    if not raw:
        return fallback
    d = date.fromisoformat(raw)
    if not date(2000, 1, 1) <= d <= today().replace(year=today().year + 30):
        raise inbox.InboxError("Sprawdź datę")
    return d


@router.post("/{att_id}/zapisz", dependencies=[Depends(verify_csrf)])
async def finish(request: Request, att_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    att = _att(db, att_id)
    form = await request.form()
    kind = att.capture_kind or "other"
    try:
        amount = parse_pln(form.get("amount"))
        if kind in ("service", "tyres"):
            done = inbox.finish_service(
                db,
                att,
                user,
                item_raw=form.get("item_id"),
                task_raw=form.get("task_id"),
                title=str(form.get("title") or ""),
                done_on=_date(form.get("done_on"), _taken_on(att)),
                amount_gr=amount,
                next_key=str(form.get("next") or "keep"),
                plan_next=form.get("plan_next") == "1",
            )
        elif kind == "receipt":
            done = inbox.finish_receipt(
                db,
                att,
                user,
                item_raw=form.get("item_id"),
                amount_gr=amount,
                booked_on=_date(form.get("booked_on"), _taken_on(att)),
                category_raw=form.get("category_id"),
                note=str(form.get("note") or ""),
            )
        elif kind == "policy":
            ends = str(form.get("ends_on") or "").strip()
            done = inbox.finish_policy(
                db,
                att,
                user,
                item_raw=form.get("item_id"),
                name=str(form.get("name") or ""),
                ends_on=_date(ends, today()) if ends else None,
                doc_kind=str(form.get("doc_kind") or "policy"),
                today=today(),
            )
        else:
            done = inbox.finish_other(db, att, item_raw=form.get("item_id"), description=str(form.get("note") or ""))
    except (inbox.InboxError, InvalidAmount) as exc:
        db.rollback()
        return _error(str(exc))
    return _saved(request, done.message)


@router.post("/{att_id}/usun", dependencies=[Depends(verify_csrf)])
def delete(request: Request, att_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    att = _att(db, att_id)
    documents.delete_attachment(db, att)
    db.commit()
    return _saved(request, "Zdjęcie usunięte")


def _saved(request: Request, message: str):
    """htmx: toast + the page under it refreshed (app.js "dm-added"); without JS: back to the list."""
    if is_htmx(request):
        return quick_saved(message)
    return RedirectResponse("/zdjecia", status_code=303)
