"""/grafik – the household's week: fixed weekly slots with who takes care of them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from domownik import schedule, services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import MEMBER_KINDS, ScheduleEvent, User
from domownik.security import current_user, verify_csrf
from domownik.web import render, week_title

router = APIRouter()

WHO_MODES = ("shared", "one", "days", "weeks")


@dataclass
class WeekDay:
    date: date
    is_today: bool
    past: bool
    entries: list[schedule.Occurrence] = field(default_factory=list)

    @property
    def name(self) -> str:
        return schedule.DAY_NAMES[self.date.weekday()]

    @property
    def short(self) -> str:
        return schedule.DAY_SHORT[self.date.weekday()]


def _back(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _error(msg: str) -> HTMLResponse:
    return HTMLResponse(
        f'<p class="error">{msg}</p>',
        status_code=422,
        headers={"HX-Retarget": "#sched-error", "HX-Reswap": "innerHTML"},
    )


def _week_url(monday: date | None, who: str | None) -> str:
    q = []
    if monday is not None and monday != schedule.week_start(today()):
        q.append(f"w={monday.isoformat()}")
    if who == "me":
        q.append("who=me")
    return "/grafik" + ("?" + "&".join(q) if q else "")


@router.get("/grafik")
def week_page(
    request: Request,
    w: date | None = None,
    who: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    t = today()
    monday = schedule.week_start(w or t)
    events = schedule.load_events(db)
    occ = schedule.occurrences(
        db, monday, monday + timedelta(days=6), user_id=user.id if who == "me" else None, events=events
    )
    days = [
        WeekDay(monday + timedelta(days=i), monday + timedelta(days=i) == t, monday + timedelta(days=i) < t)
        for i in range(7)
    ]
    for o in occ:
        days[o.day.weekday()].entries.append(o)
    users = {u.id: u for u in services.active_users(db)}
    return render(
        request,
        "schedule.html",
        {
            "user": user,
            "days": days,
            "monday": monday,
            "title": week_title(monday),
            "prev": monday - timedelta(days=7),
            "next": monday + timedelta(days=7),
            "is_current": monday == schedule.week_start(t),
            "who": who if who == "me" else None,
            "events": events,
            "users": users,
            "who_label": schedule.who_label,
            "days_label": schedule.days_label,
            "t": t,
        },
    )


def _form_ctx(db: Session, user: User, ev: ScheduleEvent | None) -> dict:
    tree = Tree.load(db)
    # people first (children, then adults), then the rest of the tree
    people = sorted(
        (i for i in tree.items.values() if i.is_member), key=lambda i: (MEMBER_KINDS.index(i.kind) * -1, i.name)
    )
    others = sorted((i for i in tree.items.values() if not i.is_member), key=lambda i: tree.label(i.id))
    mode = "shared"
    week_a = week_b = None
    if ev is not None:
        if ev.assign_mode == "days":
            mode = "days"
        elif ev.assign_mode == "weeks":
            mode = "weeks"
            week_a, week_b = schedule.on_duty_this_week(ev, today())
        elif ev.assignee_id:
            mode = "one"
    return {
        "user": user,
        "ev": ev,
        "users": services.active_users(db),
        "people": people,
        "others": others,
        "label": tree.label,
        "mode": mode,
        "week_a": week_a,
        "week_b": week_b,
        "day_names": schedule.DAY_NAMES,
        "day_short": schedule.DAY_SHORT,
    }


@router.get("/grafik/nowe")
def new_event(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "schedule_form.html", _form_ctx(db, user, None))


def _get(db: Session, ev_id: int) -> ScheduleEvent:
    ev = db.get(ScheduleEvent, ev_id)
    if ev is None:
        raise HTTPException(404, "Nie ma takiego wpisu w grafiku")
    return ev


@router.get("/grafik/{ev_id}")
def edit_event(request: Request, ev_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "schedule_form.html", _form_ctx(db, user, _get(db, ev_id)))


def _user_id(raw, valid: set[int]) -> int | None:
    raw = str(raw or "").strip()
    if raw.isdigit() and int(raw) in valid:
        return int(raw)
    return None


def _date(raw) -> date | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError("Zła data") from None


def apply_form(db: Session, ev: ScheduleEvent, form, t: date) -> None:
    """Fill the event from the form; ValueError with a Polish message on bad input."""
    title = str(form.get("title") or "").strip()[:120]
    if not title:
        raise ValueError("Podaj nazwę, np. Basen")
    days = sorted({int(d) for d in form.getlist("days") if str(d).isdigit() and int(d) < 7})
    if not days:
        raise ValueError("Zaznacz co najmniej jeden dzień tygodnia")
    starts_at = schedule.parse_time(form.get("starts_at"))
    if starts_at is None:
        raise ValueError("Podaj godzinę rozpoczęcia")
    ends_at = schedule.parse_time(form.get("ends_at"))
    if ends_at is not None and ends_at <= starts_at:
        raise ValueError("Koniec musi być po początku")
    starts_on, ends_on = _date(form.get("starts_on")), _date(form.get("ends_on"))
    if starts_on and ends_on and ends_on < starts_on:
        raise ValueError("Data końca jest wcześniejsza niż początku")
    valid = {u.id for u in services.active_users(db)}
    mode = form.get("who_mode") if form.get("who_mode") in WHO_MODES else "shared"
    ev.assignee_id = ev.alt_assignee_id = ev.rota_anchor = None
    ev.day_assignees = {}
    if mode == "one":
        ev.assign_mode = "fixed"
        ev.assignee_id = _user_id(form.get("assignee_id"), valid)
    elif mode == "days":
        ev.assign_mode = "days"
        ev.day_assignees = {str(d): _user_id(form.get(f"day_{d}"), valid) for d in days}
    elif mode == "weeks":
        a, b = _user_id(form.get("week_a"), valid), _user_id(form.get("week_b"), valid)
        if a is None or b is None or a == b:
            raise ValueError("Naprzemiennie: wybierz dwie różne osoby")
        ev.assign_mode = "weeks"
        ev.assignee_id, ev.alt_assignee_id = a, b
        ev.rota_anchor = schedule.week_start(t)  # "this week: a" – the form always shows it that way
    else:
        ev.assign_mode = "fixed"
    raw_item = str(form.get("item_id") or "").strip()
    ev.item_id = int(raw_item) if raw_item.isdigit() and Tree.load(db).items.get(int(raw_item)) else None
    ev.title = title
    ev.weekdays = ",".join(str(d) for d in days)
    ev.starts_at, ev.ends_at = starts_at, ends_at
    ev.starts_on, ev.ends_on = starts_on, ends_on
    ev.notify = bool(form.get("notify"))
    ev.notes = str(form.get("notes") or "").strip()[:2000] or None


@router.post("/grafik", dependencies=[Depends(verify_csrf)])
async def create_event(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    ev = ScheduleEvent(created_by_id=user.id)
    try:
        apply_form(db, ev, form, today())
    except ValueError as exc:
        return _error(str(exc))
    db.add(ev)
    db.commit()
    return _back("/grafik")


@router.post("/grafik/{ev_id}", dependencies=[Depends(verify_csrf)])
async def update_event(request: Request, ev_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ev = _get(db, ev_id)
    form = await request.form()
    try:
        apply_form(db, ev, form, today())
    except ValueError as exc:
        db.rollback()
        return _error(str(exc))
    db.commit()
    return _back("/grafik")


@router.post("/grafik/{ev_id}/usun", dependencies=[Depends(verify_csrf)])
def delete_event(ev_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(_get(db, ev_id))
    db.commit()
    return _back("/grafik")


@router.post("/grafik/{ev_id}/odwolaj", dependencies=[Depends(verify_csrf)])
async def toggle_cancel(
    request: Request, ev_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Call one occurrence off ("odwołane w tym tygodniu") or bring it back."""
    ev = _get(db, ev_id)
    form = await request.form()
    try:
        day = _date(form.get("d"))
        if day is None:
            raise ValueError("Brak daty")
        schedule.toggle_skip(db, ev, day, user)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    db.commit()
    back = str(form.get("back") or "")
    if back == "/":
        return _back("/")
    return _back(_week_url(schedule.week_start(day), form.get("who")))
