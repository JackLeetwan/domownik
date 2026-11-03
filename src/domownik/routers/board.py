"""Household-wide tools: sticky notes (/notatki) and all 'some day' todos (/do-zrobienia)."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.money import parse_pln
from domownik.domain.tree import Tree
from domownik.models import BoardNote, Todo, User
from domownik.security import current_user, verify_csrf
from domownik.web import is_quick, quick_saved, render

router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _date(v) -> date | None:
    v = str(v or "").strip()
    return date.fromisoformat(v) if v else None


def _int(v) -> int | None:
    v = str(v or "").strip()
    return int(v) if v else None


# ---------------------------------------------------------------- notes


@router.get("/notatki")
def notes_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = today()
    active = services.active_notes(db, t)
    active_ids = {n.id for n in active}
    archive = sorted(
        (n for n in db.scalars(select(BoardNote)) if n.id not in active_ids),
        key=lambda n: n.updated_at or n.created_at,
        reverse=True,
    )[:50]
    return render(request, "notes.html", {"user": user, "notes": active, "archive": archive})


@router.post("/notatki", dependencies=[Depends(verify_csrf)])
async def add_note(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    body = str(form.get("body") or "").strip()
    if not body:
        raise HTTPException(400, "Notatka jest pusta")
    db.add(
        BoardNote(
            body=body,
            pinned=bool(form.get("pinned")),
            valid_until=_date(form.get("valid_until")),
            created_by_id=user.id,
        )
    )
    db.commit()
    if is_quick(request):
        return quick_saved("Dodano notatkę")
    return _redirect("/notatki")


def _note(db: Session, note_id: int) -> BoardNote:
    n = db.get(BoardNote, note_id)
    if n is None:
        raise HTTPException(404, "Nie ma takiej notatki")
    return n


@router.post("/notatki/{note_id}/edit", dependencies=[Depends(verify_csrf)])
async def edit_note(request: Request, note_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    n = _note(db, note_id)
    form = await request.form()
    n.body = str(form.get("body") or "").strip() or n.body
    n.valid_until = _date(form.get("valid_until"))
    n.pinned = bool(form.get("pinned"))
    n.updated_at = datetime.now()
    db.commit()
    return _redirect("/notatki")


@router.post("/notatki/{note_id}/pin", dependencies=[Depends(verify_csrf)])
def pin_note(note_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    n = _note(db, note_id)
    n.pinned = not n.pinned
    db.commit()
    return _redirect("/notatki")


@router.post("/notatki/{note_id}/archive", dependencies=[Depends(verify_csrf)])
def archive_note(note_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    n = _note(db, note_id)
    if n.is_active(today()):
        n.archived = True
    else:  # restore: un-archive and drop an expired date
        n.archived = False
        if n.valid_until and n.valid_until < today():
            n.valid_until = None
    n.updated_at = datetime.now()
    db.commit()
    return _redirect("/notatki")


@router.post("/notatki/{note_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_note(note_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(_note(db, note_id))
    db.commit()
    return _redirect("/notatki")


# ---------------------------------------------------------------- todos


@router.get("/do-zrobienia")
def todos_page(
    request: Request, who: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    tree = Tree.load(db)
    todos = services.open_todos(db, tree)
    if who == "me":
        todos = [t for t in todos if t.assignee_id in (None, user.id)]
    groups: dict[str, list[Todo]] = {}
    for td in todos:
        key = tree.label(tree.root_of(td.item_id).id) if td.item_id and tree.root_of(td.item_id) else "Ogólne"
        groups.setdefault(key, []).append(td)
    done = sorted(
        db.scalars(select(Todo).where(Todo.done_on.is_not(None))), key=lambda t: (t.done_on, t.id), reverse=True
    )[:30]
    return render(
        request,
        "todos.html",
        {
            "user": user,
            "groups": groups,
            "count": len(todos),
            "done_todos": done,
            "who": who,
            "users": list(db.scalars(select(User).where(User.is_active.is_(True)))),
            "items": sorted(tree.items.values(), key=lambda i: tree.label(i.id)),
            "label": tree.label,
        },
    )


@router.post("/do-zrobienia", dependencies=[Depends(verify_csrf)])
async def add_todo(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    title = str(form.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Wpisz, co jest do zrobienia")
    item_id = _int(form.get("item_id"))
    if item_id is not None and item_id not in Tree.load(db).items:
        raise HTTPException(400, "Nie ma takiego elementu")
    db.add(
        Todo(
            item_id=item_id,
            title=title,
            notes=str(form.get("notes") or "").strip() or None,
            est_amount_gr=parse_pln(form.get("amount")),
            assignee_id=_int(form.get("assignee_id")),
            created_by_id=user.id,
        )
    )
    db.commit()
    if is_quick(request):
        return quick_saved("Dodano do zrobienia")
    return _redirect("/do-zrobienia")
