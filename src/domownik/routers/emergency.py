"""/na-wypadek – the family's folder in case something happens to the parents (see domownik.emergency)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import emergency
from domownik.config import get_settings, now, today
from domownik.db import get_db
from domownik.models import EMERGENCY_SECTIONS, EmergencyEntry, Todo, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/na-wypadek")

# what the two of you can do now so the family has it easier – each can go to "Do zrobienia"
PREPARE: dict[str, tuple[str, str]] = {
    "testament": (
        "Spisać testament i wskazać opiekuna dla dzieci",
        "Testament własnoręczny (cały odręcznie, data, podpis) albo u notariusza. Sąd rodzinny bierze pod uwagę "
        "osobę wskazaną przez rodziców jako opiekuna.",
    ),
    "beneficiaries": (
        "Sprawdzić uposażonych w polisach na życie, PPK i IKE/IKZE",
        "Pieniądze z polisy i PPK trafiają do uposażonych poza spadkiem – szybciej i bez sądu.",
    ),
    "bank": (
        "Złożyć w banku dyspozycję wkładem na wypadek śmierci",
        "Bank wypłaci wskazanej osobie część pieniędzy bez czekania na spadek (do ustawowego limitu).",
    ),
    "passwords": (
        "Ustawić dostęp awaryjny do haseł",
        "Np. Bitwarden „Emergency Access” albo zapieczętowana koperta z hasłem głównym u zaufanej osoby.",
    ),
    "accounts": (
        "Ustawić konta na wypadek śmierci: Google, Facebook, Apple",
        "Google: „Menedżer nieaktywnego konta”; Facebook: „kontakt na wypadek śmierci”; Apple: „Kontakt "
        "spadkowy”. Dzięki temu zdjęcia i poczta nie przepadną.",
    ),
    "business": (
        "Jeśli jest działalność gospodarcza – ustanowić zarządcę sukcesyjnego",
        "Wpis w CEIDG; bez niego firma po śmierci właściciela przestaje działać (rodzina ma na to tylko 2 miesiące).",
    ),
}


def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _error(msg: str) -> HTMLResponse:
    return HTMLResponse(msg, status_code=400, headers={"HX-Reswap": "none"})


def _entry(db: Session, entry_id: int) -> EmergencyEntry:
    e = db.get(EmergencyEntry, entry_id)
    if e is None:
        raise HTTPException(404, "Nie ma takiego wpisu")
    return e


SECRET_MSG = (
    "To wygląda na hasło, PIN albo numer karty – nie wpisuj ich tutaj, ta kartka trafi do rodziny. "
    "Napisz raczej, gdzie je znaleźć."
)


def _fill(e: EmergencyEntry, form) -> str | None:
    section = str(form.get("section") or e.section or "")
    if section not in EMERGENCY_SECTIONS:
        return "Wybierz dział"
    title = emergency.clean_text(form.get("title"), 200)
    body = emergency.clean_text(form.get("body"), 5000)
    if not title:
        return "Wpisz, czego dotyczy wpis – np. „mBank, konto wspólne”"
    if emergency.looks_secret(title) or emergency.looks_secret(body):
        return SECRET_MSG
    e.section, e.title, e.body = section, title, body or None
    return None


def _todos(db: Session) -> dict[str, Todo]:
    titles = {t: k for k, (t, _) in PREPARE.items()}
    found = db.scalars(select(Todo).where(Todo.title.in_(titles))).all()
    return {titles[t.title]: t for t in found}


@router.get("")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    fresh = emergency.freshness(db, now())
    return render(
        request,
        "emergency.html",
        {
            "user": user,
            "SECTIONS": EMERGENCY_SECTIONS,
            "entries": emergency.entries(db),
            "auto": emergency.overview(db, today()),
            "fresh": fresh,
            "printed_local": emergency.local(fresh.printed.printed_at, get_settings().tz) if fresh.printed else None,
            "review": emergency.review_task(db),
            "PREPARE": PREPARE,
            "prepared": _todos(db),
        },
    )


@router.post("/wpisy", dependencies=[Depends(verify_csrf)])
async def create(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    e = EmergencyEntry(created_by_id=user.id)
    if err := _fill(e, form):
        return _error(err)
    emergency.touch(e, user, now())
    db.add(e)
    db.commit()
    return _redirect(f"/na-wypadek#{e.section}")


@router.post("/wpisy/{entry_id}", dependencies=[Depends(verify_csrf)])
async def update(request: Request, entry_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    e = _entry(db, entry_id)
    form = await request.form()
    if err := _fill(e, form):
        db.rollback()
        return _error(err)
    emergency.touch(e, user, now())
    db.commit()
    return _redirect(f"/na-wypadek#{e.section}")


@router.post("/wpisy/{entry_id}/usun", dependencies=[Depends(verify_csrf)])
def delete(entry_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    e = _entry(db, entry_id)
    section = e.section
    db.delete(e)
    db.commit()
    return _redirect(f"/na-wypadek#{section}")


@router.post("/przypomnienie", dependencies=[Depends(verify_csrf)])
def add_reminder(db: Session = Depends(get_db), user: User = Depends(current_user)):
    if emergency.add_review_task(db, today()) is None:
        return _error("Najpierw utwórz choć jedną strefę – przypomnienie musi gdzieś należeć")
    db.commit()
    return _redirect("/na-wypadek#wydruk")


@router.post("/przygotuj/{key}", dependencies=[Depends(verify_csrf)])
def prepare(key: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if key not in PREPARE:
        raise HTTPException(404)
    if key not in _todos(db):
        title, notes = PREPARE[key]
        db.add(Todo(title=title, notes=notes, created_by_id=user.id))
        db.commit()
    return _redirect("/na-wypadek#przygotowanie")


@router.post("/druk", dependencies=[Depends(verify_csrf)])
def mark_printed(db: Session = Depends(get_db), user: User = Depends(current_user)):
    emergency.record_print(db, user, now())
    db.commit()
    resp = RedirectResponse("/na-wypadek/druk?drukuj=1", status_code=303)
    resp.headers["HX-Redirect"] = "/na-wypadek/druk?drukuj=1"  # a full page load: the printout has no app shell
    return resp


@router.get("/druk")
def printout(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id)).all()
    return render(
        request,
        "emergency_print.html",
        {
            "user": user,
            "nav": None,
            "SECTIONS": EMERGENCY_SECTIONS,
            "entries": emergency.entries(db),
            "auto": emergency.overview(db, today()),
            "authors": [u.name for u in users],
            "stamp": now(),
            "autoprint": request.query_params.get("drukuj") == "1",
        },
    )
