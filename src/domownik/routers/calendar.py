"""Month calendar (/kalendarz) and the search box (/szukaj)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from domownik import schedule, services
from domownik import search as search_mod
from domownik.config import today
from domownik.db import get_db
from domownik.domain.recurrence import MONTH_GENITIVE
from domownik.domain.tree import Tree
from domownik.models import User
from domownik.security import current_user
from domownik.web import render

router = APIRouter()

DAY_NAMES = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")


def day_title(d: date) -> str:
    """'wtorek, 10 listopada' (+ year when not the current one)."""
    text = f"{DAY_NAMES[d.weekday()]}, {d.day} {MONTH_GENITIVE[d.month]}"
    return text + (f" {d.year}" if d.year != today().year else "")


def _month(m: str | None) -> tuple[int, int]:
    if not m:
        t = today()
        return t.year, t.month
    try:
        y, mo = (int(x) for x in m.split("-"))
        date(y, mo, 1)
    except ValueError:
        raise HTTPException(400, "Miesiąc w formacie RRRR-MM") from None
    if not 1900 <= y <= 2200:
        raise HTTPException(400, "Miesiąc poza zakresem")
    return y, mo


def _who(who: str | None, user: User) -> int | None:
    return user.id if who == "me" else None


def _slots(db: Session, day: date, who: str | None, user: User) -> list[schedule.Occurrence]:
    """Week-schedule slots of the selected day – only in the day list, the month grid stays uncluttered."""
    return schedule.occurrences(db, day, day, user_id=_who(who, user))


@router.get("/kalendarz")
def calendar_page(
    request: Request,
    m: str | None = None,
    d: date | None = None,
    who: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    t = today()
    services.settle_autopay(db, t)
    if d is not None and not m:
        y, mo = d.year, d.month
    else:
        y, mo = _month(m)
    tree = Tree.load(db)
    cal = services.calendar_month(db, tree, t, y, mo, user_id=_who(who, user))
    sel = d if d is not None and cal.weeks[0][0].date <= d <= cal.weeks[-1][-1].date else cal.default_day(t)
    sel_day = next((x for x in cal.days() if x.date == sel), None)
    entries = sel_day.entries if sel_day else services.calendar_day(db, tree, t, sel, user_id=_who(who, user))
    return render(
        request,
        "calendar.html",
        {
            "user": user,
            "cal": cal,
            "weekdays": services.CAL_WEEKDAYS,
            "sel": sel,
            "sel_title": day_title(sel),
            "entries": entries,
            "slots": _slots(db, sel, who, user),
            "who": who if who == "me" else None,
            "any_assigned": services.any_assigned(db),
        },
    )


@router.get("/kalendarz/dzien")
def calendar_day(
    request: Request,
    d: date,
    who: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Tasks of one day – the list under the grid (swapped in when a day is tapped)."""
    entries = services.calendar_day(db, Tree.load(db), today(), d, user_id=_who(who, user))
    return render(
        request,
        "partials/cal_day.html",
        {"user": user, "sel": d, "sel_title": day_title(d), "entries": entries, "slots": _slots(db, d, who, user)},
    )


@router.get("/kalendarz/grupa")
def calendar_group(
    request: Request,
    d: date,
    of: str,
    cat: str,
    who: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Window with the tasks behind one chip ("💳 Opłaty 17")."""
    entries = services.calendar_day(db, Tree.load(db), today(), d, user_id=_who(who, user))
    group = next((e for e in entries if e["kind"] == "bundle" and e["of"] == of and e["category"] == cat), None)
    if group is None:
        raise HTTPException(404, "Nie ma już takiej grupy – odśwież stronę")
    return render(request, "partials/cal_group.html", {"user": user, "g": group, "sel_title": day_title(d)})


# ---------------------------------------------------------------- search

LIVE_PER_GROUP = 4


@router.get("/szukaj")
def search_page(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    res = search_mod.search(db, q)
    return render(
        request,
        "search.html",
        {
            "user": user,
            "res": res,
            "q": q.strip(),
            "KINDS": search_mod.KINDS,
            "limit": None,
            "too_short": bool(q.strip()) and not res.groups and len(q.strip()) < search_mod.MIN_QUERY,
        },
    )


@router.get("/szukaj/na-zywo")
def search_live(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Results under the search box while typing (a few per group + link to the full page)."""
    res = search_mod.search(db, q)
    return render(
        request,
        "partials/search_results.html",
        {
            "user": user,
            "res": res,
            "q": q.strip(),
            "KINDS": search_mod.KINDS,
            "limit": LIVE_PER_GROUP,
            "live": True,
            "too_short": len(q.strip()) < search_mod.MIN_QUERY,
        },
    )
