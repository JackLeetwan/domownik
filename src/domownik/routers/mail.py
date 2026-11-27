"""/poczta – ✉️ emails from the kindergarten waiting in 📥 Do rozpoznania (see domownik.mailwatch)."""

from __future__ import annotations

from datetime import date, time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from domownik import inbox, mailwatch
from domownik.config import now
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import MailMessage, User
from domownik.security import current_user, verify_csrf

router = APIRouter(prefix="/poczta")


def _msg(db: Session, msg_id: int) -> MailMessage:
    m = db.get(MailMessage, msg_id)
    if m is None or m.status != "new":
        raise HTTPException(404, "Tego maila już nie ma w „Do rozpoznania”")
    return m


def card_ctx(db: Session, m: MailMessage, tree: Tree | None = None) -> dict:
    tree = tree or Tree.load(db)
    res = mailwatch.proposals(db, m)
    bring: list[str] = []
    for p in res.proposals:
        bring += [b for b in p.bring if b not in bring]
    top, rest = inbox.item_choices(db, tree)
    default = mailwatch.default_item(db)
    return {
        "m": m,
        "res": res,
        "bring": ", ".join(bring),
        "targets": top + rest,
        "label": tree.label,
        "default_item_id": default.id if default else None,
    }


def page_ctx(db: Session) -> dict:
    tree = Tree.load(db)
    st = db.get(mailwatch.MailState, 1)
    return {
        "mail_on": mailwatch.configured(),
        "mail_cards": [card_ctx(db, m, tree) for m in mailwatch.pending(db)],
        "mail_state": st,
        "mail_check_at": mailwatch.check_time(),
        "mail_label": mailwatch.get_settings().mail_label,
    }


def _back(note: str | None = None) -> RedirectResponse:
    url = "/zdjecia" + (f"?poczta={note}" if note else "") + "#poczta"
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _error(msg: str) -> HTMLResponse:
    return HTMLResponse(msg, status_code=400, headers={"HX-Reswap": "none"})


def _date(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw or "").strip())
    except ValueError:
        return None


def _time(raw) -> time | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return time.fromisoformat(s)
    except ValueError:
        raise mailwatch.MailError(f"Nie rozumiem godziny „{s[:10]}”") from None


@router.post("/sprawdz", dependencies=[Depends(verify_csrf)])
def check_now(db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not mailwatch.configured():
        return _error("Sprawdzanie poczty nie jest włączone (DOMOWNIK_MAIL_USER / _PASSWORD / _FROM)")
    n = mailwatch.check(db, now(), push=False)
    return _back("nowe-" + str(n))


@router.post("/{msg_id}", dependencies=[Depends(verify_csrf)])
async def save(request: Request, msg_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = _msg(db, msg_id)
    form = await request.form()
    res = mailwatch.proposals(db, m)
    entries: list[mailwatch.Entry] = []
    try:
        for i, p in enumerate(res.proposals):
            if not form.get(f"pick_{i}"):
                continue
            day = _date(form.get(f"day_{i}"))
            if day is None:
                raise mailwatch.MailError(f"„{p.title}”: podaj datę")
            end = _date(form.get(f"end_{i}")) or day
            if end < day:
                raise mailwatch.MailError(f"„{p.title}”: koniec przed początkiem")
            entries.append(
                mailwatch.Entry(
                    title=str(form.get(f"title_{i}") or "").strip(),
                    day=day,
                    end=end,
                    at=_time(form.get(f"at_{i}")),
                    icon=str(form.get(f"icon_{i}") or p.icon),
                    notes=mailwatch.note_for(m, p.paragraph),
                )
            )
        extra_title = str(form.get("extra_title") or "").strip()
        if extra_title:  # a day the rules missed, typed in by hand
            day = _date(form.get("extra_day"))
            if day is None:
                raise mailwatch.MailError(f"„{extra_title}”: podaj datę")
            entries.append(
                mailwatch.Entry(
                    extra_title, day, day, _time(form.get("extra_at")), "🏫", mailwatch.note_for(m, m.body[:1500])
                )
            )
        shopping = [s.strip() for s in str(form.get("shopping") or "").split(",") if s.strip()]
        if not entries and not shopping:
            raise mailwatch.MailError(
                "Nic nie zaznaczone – jeśli w mailu nie ma nic do dopisania, użyj „Nic do dopisania”"
            )
        raw_item = str(form.get("item_id") or "")
        mailwatch.apply(
            db, m, entries, item_id=int(raw_item) if raw_item.isdigit() else None, shopping=shopping, user=user
        )
    except mailwatch.MailError as exc:
        db.rollback()
        return _error(str(exc))
    db.commit()
    return _back("zapisane")


@router.post("/{msg_id}/odrzuc", dependencies=[Depends(verify_csrf)])
def dismiss(msg_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    mailwatch.dismiss(db, _msg(db, msg_id), user)
    db.commit()
    return _back()
