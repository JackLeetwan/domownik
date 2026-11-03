"""Comments under a task (its window) or a note (its card) – with a push to the person it concerns."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import notify, webpush
from domownik.db import get_db
from domownik.models import NOTE_COLORS, BoardNote, Comment, PushSubscription, Task, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/komentarze")
log = logging.getLogger("domownik.comments")
MAX_LEN = 1000


def _body(form) -> str:
    text = str(form.get("body") or "").strip()[:MAX_LEN]
    if not text:
        raise HTTPException(400, "Komentarz jest pusty")
    return text


def _tell(db: Session, who: User | None, author: User, title: str, text: str, url: str) -> None:
    """Push to the person the thing concerns (not to the author). Never breaks the request."""
    if who is None or who.id == author.id or not who.is_active:
        return
    if db.scalar(select(PushSubscription.id).where(PushSubscription.user_id == who.id).limit(1)) is None:
        return
    body = {
        "title": title,
        "body": f"{author.name}: {text[:140]}",
        "url": url,
        "tag": f"domownik-comment-{url}",
        "actions": [],
    }
    try:
        notify.send(db, who, body)
    except webpush.PushError as exc:
        log.warning("push: %s – %s", who.name, exc)
    except Exception:  # a comment must be saved even if pushing breaks
        log.exception("comment push failed")


def _task_fragment(request: Request, task: Task, user: User) -> HTMLResponse:
    return render(request, "partials/comments.html", {"user": user, "owner": task, "kind": "zadanie"})


def _note_fragment(request: Request, note: BoardNote, user: User) -> HTMLResponse:
    return render(
        request, "partials/note_card.html", {"user": user, "n": note, "NOTE_COLORS": NOTE_COLORS, "comments_open": True}
    )


@router.post("/zadanie/{task_id}", dependencies=[Depends(verify_csrf)])
async def on_task(request: Request, task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Nie ma takiego zadania")
    text = _body(await request.form())
    task.comments.append(Comment(body=text, user_id=user.id))
    db.commit()
    _tell(db, task.assignee, user, f"💬 {task.title}", text, f"/tasks/{task.id}")
    db.refresh(task)
    return _task_fragment(request, task, user)


@router.post("/notatka/{note_id}", dependencies=[Depends(verify_csrf)])
async def on_note(request: Request, note_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    note = db.get(BoardNote, note_id)
    if note is None:
        raise HTTPException(404, "Nie ma takiej notatki")
    text = _body(await request.form())
    note.comments.append(Comment(body=text, user_id=user.id))
    db.commit()
    _tell(db, note.created_by, user, f"💬 {note.title or 'Notatka'}", text, "/notatki")
    db.refresh(note)
    return _note_fragment(request, note, user)


@router.post("/{cid}/usun", dependencies=[Depends(verify_csrf)])
def delete(request: Request, cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(Comment, cid)
    if c is None:
        raise HTTPException(404, "Nie ma takiego komentarza")
    if c.user_id != user.id:
        raise HTTPException(403, "Usunąć można tylko własny komentarz")
    task_id, note_id = c.task_id, c.note_id
    db.delete(c)
    db.commit()
    if task_id:
        return _task_fragment(request, db.get(Task, task_id), user)
    return _note_fragment(request, db.get(BoardNote, note_id), user)
