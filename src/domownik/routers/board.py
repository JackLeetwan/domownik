"""Household-wide tools: sticky notes (/notatki) and all 'some day' todos (/do-zrobienia)."""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import documents, privacy, services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.money import parse_pln
from domownik.domain.tree import Tree
from domownik.models import NOTE_COLORS, Attachment, BoardNote, NoteLabelColor, Todo, User, note_label_shades
from domownik.security import current_user, verify_csrf
from domownik.web import is_quick, quick_saved, render

router = APIRouter()


HEX_COLOR = re.compile(r"#[0-9a-f]{6}")  # a colour from the palette, stored as is


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


DONE_MARKS = ("✓ ", "✓", "[x] ", "[x]", "[X] ", "[X]")
OPEN_MARKS = ("☐ ", "☐", "[ ] ", "[ ]")  # the edit form shows open points as "☐ …"
MAX_NOTE_ITEMS = 200
NO_LABEL = "_brak"


def parse_items(text: str) -> list[dict]:
    """One point per line; a line starting with ✓ or [x] is already ticked, ☐ / [ ] or nothing – open."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip().lstrip("-•*").strip()
        done = False
        for mark in DONE_MARKS:
            if line.startswith(mark):
                line, done = line[len(mark) :].strip(), True
                break
        else:
            for mark in OPEN_MARKS:
                if line.startswith(mark):
                    line = line[len(mark) :].strip()
                    break
        if line:
            out.append({"t": line[:200], "d": done})
    return out[:MAX_NOTE_ITEMS]


def _fill(n: BoardNote, form, user: User, *, has_photos: bool = False) -> None:
    """Title, content (text or checklist), colour, label, pin, date – from the add / edit form."""
    n.title = str(form.get("title") or "").strip()[:120] or None
    content = str(form.get("body") or "").strip()
    if form.get("as_list"):
        n.items, n.body = parse_items(content), ""
    else:
        # a checklist turned into plain text: drop the "☐ " the edit form put in front of open points
        content = "\n".join(ln[2:] if ln.startswith("☐ ") else ln for ln in content.splitlines())
        n.items, n.body = None, content
    if not (n.title or n.body or n.items or has_photos):  # a photo alone is a note too
        raise HTTPException(400, "Notatka jest pusta")
    color = form.get("color")
    n.color = color if color in NOTE_COLORS else None  # "" = by its label
    n.label = str(form.get("label") or "").strip()[:40] or None
    n.pinned = bool(form.get("pinned"))
    n.valid_until = _date(form.get("valid_until"))
    privacy.apply(n, form, user)


MAX_NOTE_PHOTOS = 12


def _store_photos(db: Session, n: BoardNote, form, user: User) -> None:
    """Photos picked in the note's form or on its card – all saved or (rolled back) none; 422 with the reason."""
    files = documents.uploads_in(form)
    if not files:
        return
    if len(n.photos) + len(files) > MAX_NOTE_PHOTOS:
        db.rollback()
        raise HTTPException(422, f"Notatka może mieć najwyżej {MAX_NOTE_PHOTOS} zdjęć")
    try:
        documents.store_files(db, None, [(f.filename or "", f.file) for f in files], user=user, note_id=n.id)
    except documents.UploadError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None


def note_labels(notes: list[BoardNote]) -> list[str]:
    return sorted({n.label for n in notes if n.label}, key=str.lower)


@router.get("/notatki")
def notes_page(
    request: Request, k: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    t = today()
    active = services.active_notes(db, t)
    active_ids = {n.id for n in active}
    archive = sorted(
        (n for n in db.scalars(select(BoardNote)) if n.id not in active_ids),
        key=lambda n: n.updated_at or n.created_at,
        reverse=True,
    )[:50]
    labels = note_labels(active)
    shown = active
    if k == NO_LABEL:
        shown = [n for n in active if not n.label]
    elif k:
        shown = [n for n in active if n.label == k]
    return render(
        request,
        "notes.html",
        {
            "user": user,
            "notes": shown,
            "total": len(active),
            "archive": archive,
            "labels": labels,
            "label_shades": note_label_shades(db),
            "custom_label_colors": set(db.scalars(select(NoteLabelColor.label))),
            "all_labels": note_labels(active + archive),
            "k": k,
            "NO_LABEL": NO_LABEL,
            "NOTE_COLORS": NOTE_COLORS,
        },
    )


@router.post("/notatki/kategoria-kolor", dependencies=[Depends(verify_csrf)])
async def set_label_color(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The colour of a whole category – its notes without their own colour and its chip. "" = automatic again."""
    form = await request.form()
    label = str(form.get("label") or "").strip()[:40]
    if not label:
        raise HTTPException(400, "Brak kategorii")
    # a swatch / "↺ automatyczny" button sends color; the palette (<input type=color>) sends only custom
    color = str(form.get("color") or "") if "color" in form else str(form.get("custom") or "").lower()
    row = db.get(NoteLabelColor, label)
    if color in NOTE_COLORS or HEX_COLOR.fullmatch(color):
        if row is None:
            db.add(NoteLabelColor(label=label, color=color))
        else:
            row.color = color
    elif row is not None:
        db.delete(row)
    db.commit()
    return _redirect(f"/notatki?k={quote(label)}")


@router.post("/notatki", dependencies=[Depends(verify_csrf)])
async def add_note(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    n = BoardNote(created_by_id=user.id)
    _fill(n, form, user, has_photos=bool(documents.uploads_in(form)))
    db.add(n)
    db.flush()
    _store_photos(db, n, form, user)
    db.commit()
    if is_quick(request):
        return quick_saved("Dodano notatkę")
    return _redirect("/notatki" + (f"?k={quote(n.label)}" if n.label and form.get("k") else ""))


def _note(db: Session, note_id: int) -> BoardNote:
    n = db.get(BoardNote, note_id)
    if n is None:
        raise HTTPException(404, "Nie ma takiej notatki")
    return n


@router.post("/notatki/{note_id}/edit", dependencies=[Depends(verify_csrf)])
async def edit_note(request: Request, note_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    n = _note(db, note_id)
    form = await request.form()
    _fill(n, form, user, has_photos=bool(n.photos or documents.uploads_in(form)))
    n.updated_at = datetime.now()
    _store_photos(db, n, form, user)
    db.commit()
    return _redirect("/notatki")


@router.post("/notatki/{note_id}/zdjecia", dependencies=[Depends(verify_csrf)])
async def add_note_photos(
    request: Request, note_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    n = _note(db, note_id)
    _store_photos(db, n, await request.form(), user)
    n.updated_at = datetime.now()
    db.commit()
    db.refresh(n)
    return _card(request, n, user)


@router.post("/notatki/{note_id}/zdjecia/{att_id}/usun", dependencies=[Depends(verify_csrf)])
def delete_note_photo(
    request: Request, note_id: int, att_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    n = _note(db, note_id)
    att = db.get(Attachment, att_id)
    if att is None or att.note_id != n.id:
        raise HTTPException(404, "Nie ma takiego zdjęcia")
    documents.delete_attachment(db, att)
    db.commit()
    db.refresh(n)
    return _card(request, n, user)


def _card(request: Request, n: BoardNote, user: User):
    return render(request, "partials/note_card.html", {"n": n, "user": user, "NOTE_COLORS": NOTE_COLORS})


@router.post("/notatki/{note_id}/punkt/{idx}", dependencies=[Depends(verify_csrf)])
def toggle_note_item(
    request: Request, note_id: int, idx: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    n = _note(db, note_id)
    items = [dict(i) for i in n.items or []]
    if not 0 <= idx < len(items):
        raise HTTPException(404, "Nie ma takiego punktu")
    items[idx]["d"] = not items[idx].get("d")
    n.items = items  # a new list – JSON columns do not track in-place changes
    n.updated_at = datetime.now()
    db.commit()
    return _card(request, n, user)


@router.post("/notatki/{note_id}/punkt", dependencies=[Depends(verify_csrf)])
async def add_note_item(
    request: Request, note_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    n = _note(db, note_id)
    form = await request.form()
    new = parse_items(str(form.get("t") or "").replace("✓", ""))
    if n.items is None:
        raise HTTPException(400, "To nie jest lista")
    n.items = ([dict(i) for i in n.items] + new)[:MAX_NOTE_ITEMS]
    n.updated_at = datetime.now()
    db.commit()
    return _card(request, n, user)


@router.post("/notatki/{note_id}/wyczysc", dependencies=[Depends(verify_csrf)])
def clear_done_items(request: Request, note_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Drop the ticked points of a list note."""
    n = _note(db, note_id)
    n.items = [dict(i) for i in n.items or [] if not i.get("d")]
    n.updated_at = datetime.now()
    db.commit()
    return _card(request, n, user)


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
    n = _note(db, note_id)
    for att in list(n.photos):
        documents.delete_attachment(db, att)  # the files too, not only the rows
    db.delete(n)
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
    todo = Todo(
        item_id=item_id,
        title=title,
        notes=str(form.get("notes") or "").strip() or None,
        est_amount_gr=parse_pln(form.get("amount")),
        assignee_id=_int(form.get("assignee_id")),
        created_by_id=user.id,
    )
    try:
        privacy.apply(todo, form, user)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    db.add(todo)
    db.commit()
    if is_quick(request):
        return quick_saved("Dodano do zrobienia")
    return _redirect("/do-zrobienia")
