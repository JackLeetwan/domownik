"""Meters of a house / flat: readings, usage, chart, cost. The car odometer is only shown (items router)."""

from __future__ import annotations

import calendar
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import documents, meters, services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import METER_KINDS, METER_UNITS, Item, Meter, MeterReading, Task, User
from domownik.security import current_user, verify_csrf
from domownik.web import error_target, is_quick, quick_saved, render

router = APIRouter()

REMINDER_PREFIX = "Spisz licznik: "


def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _error(request: Request, msg: str, target: str) -> HTMLResponse:
    return HTMLResponse(
        f'<p class="error">{escape(msg)}</p>',
        status_code=422,
        headers={"HX-Retarget": error_target(request, target), "HX-Reswap": "innerHTML"},
    )


def _get(db: Session, meter_id: int) -> Meter:
    m = db.get(Meter, meter_id)
    if m is None:
        raise HTTPException(404, "Nie ma takiego licznika")
    return m


def readings_of(m: Meter) -> list[meters.Reading]:
    return [meters.Reading(r.read_on, r.value, r.is_reset, r.id) for r in m.readings]


def meter_stats(m: Meter, t: date) -> meters.Stats:
    return meters.compute(readings_of(m), unit=m.unit, today=t, unit_price=m.unit_price)


def odometer_stats(db: Session, item: Item, t: date) -> meters.Stats | None:
    """The car's mileage in the meter view – read from the same readings the task forecasts use."""
    km = services.km_readings(db, {item.id}).get(item.id, [])
    if item.kind != "car" or not km:
        return None
    return meters.compute([meters.Reading(d, v) for d, v in km], unit="km", today=t)


def item_meters(db: Session, item: Item, t: date) -> list[tuple[Meter, meters.Stats]]:
    return [(m, meter_stats(m, t)) for m in item.meters]


def reminder_task(db: Session, m: Meter) -> Task | None:
    return db.scalar(
        select(Task).where(Task.item_id == m.item_id, Task.title == REMINDER_PREFIX + m.name, Task.active.is_(True))
    )


def _apply(m: Meter, form) -> None:
    name = str(form.get("name") or "").strip()[:80]
    kind = form.get("kind") if form.get("kind") in METER_KINDS else "other"
    unit = str(form.get("unit") or "").strip()[:10] or METER_KINDS[kind][2]
    if not name:
        name = METER_KINDS[kind][0]
    m.name, m.kind, m.unit = name, kind, unit
    price = meters.parse_number(form.get("unit_price"))
    m.unit_price = price or None


@router.post("/items/{item_id}/liczniki", dependencies=[Depends(verify_csrf)])
async def add_meter(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Nie ma takiego elementu")
    form = await request.form()
    m = Meter(item_id=item.id, sort_order=len(item.meters))
    try:
        _apply(m, form)
    except ValueError as exc:
        return _error(request, str(exc), "#meter-error-new")
    db.add(m)
    db.commit()
    return _redirect(f"/liczniki/{m.id}")


@router.get("/liczniki/{meter_id}")
def meter_page(request: Request, meter_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = _get(db, meter_id)
    t = today()
    tree = Tree.load(db, include_archived=True)
    return render(
        request,
        "meter.html",
        {
            "user": user,
            "m": m,
            "st": meter_stats(m, t),
            "path": tree.path(m.item_id),
            "rows": list(reversed(m.readings)),
            "kinds": METER_KINDS,
            "units": METER_UNITS,
            "reminder": reminder_task(db, m),
            "today": t,
            "fmt": meters.fmt_num,
            "chart": meters.bar_chart,
        },
    )


@router.post("/liczniki/{meter_id}/edit", dependencies=[Depends(verify_csrf)])
async def edit_meter(
    request: Request, meter_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    m = _get(db, meter_id)
    form = await request.form()
    old_title = REMINDER_PREFIX + m.name
    try:
        _apply(m, form)
    except ValueError as exc:
        db.rollback()
        return _error(request, str(exc), "#meter-error-edit")
    # the "spisz licznik" reminder follows the new name
    for task in db.scalars(select(Task).where(Task.item_id == m.item_id, Task.title == old_title)):
        task.title = REMINDER_PREFIX + m.name
    db.commit()
    return _redirect(f"/liczniki/{m.id}")


@router.post("/liczniki/{meter_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_meter(meter_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = _get(db, meter_id)
    item_id = m.item_id
    db.delete(m)  # readings go with it; their photos stay in the item's documents
    db.commit()
    return _redirect(f"/items/{item_id}#liczniki")


@router.post("/liczniki/odczyty", dependencies=[Depends(verify_csrf)])
async def add_reading(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    raw_id = str(form.get("meter_id") or "")
    if not raw_id.isdigit():
        return _error(request, "Wybierz licznik", "#reading-error")
    m = _get(db, int(raw_id))
    target = f"#reading-error-{m.id}" if form.get("inline") else "#reading-error"
    try:
        value = meters.parse_number(form.get("value"))
        on = date.fromisoformat(str(form.get("read_on") or "").strip() or today().isoformat())
    except ValueError as exc:
        return _error(request, str(exc).replace("Invalid isoformat string", "Zła data"), target)
    if value is None:
        return _error(request, "Wpisz stan licznika", target)
    if on > today():
        return _error(request, "Data odczytu nie może być z przyszłości", target)
    reset = bool(form.get("is_reset"))
    warning = meters.check_new(readings_of(m), on, value, reset)
    if warning:
        return _error(request, warning, target)
    reading = MeterReading(
        meter_id=m.id,
        read_on=on,
        value=value,
        is_reset=reset,
        note=str(form.get("note") or "").strip()[:200] or None,
        created_by_id=user.id,
    )
    files = documents.uploads_in(form)
    if files:
        try:
            att = documents.store_files(
                db,
                m.item,
                [(f.filename or "", f.file) for f in files[:1]],
                user=user,
                doc_kind="photo",
                description=f"Licznik {m.name}: {meters.fmt_num(value)} {m.unit} ({meters.fmt_day(on)})",
            )
        except documents.UploadError as exc:
            db.rollback()
            return _error(request, str(exc), target)
        reading.attachment_id = att[0].id
    db.add(reading)
    db.commit()
    if is_quick(request):
        return quick_saved("Dodano odczyt")
    nxt = str(form.get("next") or "")
    return _redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else f"/liczniki/{m.id}")


@router.post("/liczniki/odczyty/{reading_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_reading(reading_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = db.get(MeterReading, reading_id)
    if r is None:
        raise HTTPException(404, "Nie ma takiego odczytu")
    meter_id = r.meter_id
    db.delete(r)  # the photo stays in the item's documents
    db.commit()
    return _redirect(f"/liczniki/{meter_id}")


@router.post("/liczniki/{meter_id}/przypomnienie", dependencies=[Depends(verify_csrf)])
def add_reminder(meter_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """A plain monthly task "Spisz licznik: …" on the last day of the month – no logic of its own."""
    m = _get(db, meter_id)
    if reminder_task(db, m) is None:
        t = today()
        due = date(t.year, t.month, calendar.monthrange(t.year, t.month)[1])
        db.add(
            Task(
                item_id=m.item_id,
                title=REMINDER_PREFIX + m.name,
                category="other",
                recurrence={"mode": "monthly", "day": 31},
                lead_days=1,
                next_due_date=due,
                direction="none",
                notes="Wpisz stan w Domowniku: „＋” → Odczyt licznika.",
            )
        )
        db.commit()
    return _redirect(f"/liczniki/{m.id}")
