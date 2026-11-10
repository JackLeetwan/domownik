"""⚖️ Sylwetka in "Forma": weight, measurements and photos – only ever the logged-in person's own."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from domownik import body
from domownik.config import today
from domownik.db import get_db
from domownik.models import BodyCheck, BodyPrefs, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/forma")

RANGES = [(90, "3 mies."), (365, "Rok"), (0, "Całość")]


def _go(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _day(raw) -> date:
    raw = str(raw or "").strip()
    if not raw:
        return today()
    try:
        d = date.fromisoformat(raw)
    except ValueError as exc:
        raise body.BodyError("Nieprawidłowa data") from exc
    if d > today():
        raise body.BodyError("Wpis z przyszłości? Wybierz dzisiejszy albo wcześniejszy dzień")
    return d


def card_context(db: Session, user: User) -> dict:
    """The "⚖️ Sylwetka" card on /forma."""
    t = today()
    due, check_due = body.next_check(db, user, t)
    return {"body": body.summary(db, user, t), "check_next": due, "check_due": check_due, "bfmt": body.fmt}


# ---------------------------------------------------------------- the page


@router.get("/sylwetka")
def page(request: Request, okres: int = 90, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _page(request, db, user, okres)


def _page(
    request: Request, db: Session, user: User, okres: int = 90, error: str = "", typed: str = "", status_code: int = 200
):
    t = today()
    if okres not in dict(RANGES):
        okres = 90
    entries = body.weights(db, user)
    p = db.get(BodyPrefs, user.id)
    start = t - timedelta(days=okres) if okres else (entries[0].day if entries else t)
    cs = body.checks(db, user)
    return render(
        request,
        "body.html",
        {
            "user": user,
            "chart": body.chart(entries, p.goal_kg if p else None, start, t),
            "ranges": RANGES,
            "okres": okres,
            "recent": list(reversed(entries[-30:])),
            "checks": cs,
            "changes": body.changes(cs),
            "with_photos": sum(1 for c in cs if c.photos),
            "prefs": p,
            "every_choices": body.CHECK_EVERY_CHOICES,
            "poses": body.POSES,
            "sites": body.SITES,
            "photo_of": body.photo_of,
            "error": error,
            "typed": typed,
            "today": t,
        }
        | card_context(db, user),
        status_code=status_code,
    )


# ---------------------------------------------------------------- weight


@router.post("/waga", dependencies=[Depends(verify_csrf)])
async def add_weight(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    back = "/forma/sylwetka" if form.get("back") == "sylwetka" else "/forma"
    try:
        kg = body.parse_number(form.get("kg"), "Waga", *body.KG_RANGE, "kg")
        if kg is None:
            raise body.BodyError("Wpisz wagę w kg, np. 72,4")
        body.add_weight(db, user, _day(form.get("day")), kg)
    except body.BodyError as exc:
        db.rollback()
        return _page(request, db, user, error=str(exc), typed=str(form.get("kg") or ""), status_code=422)
    db.commit()
    return _go(back + "#sylwetka")


@router.post("/waga/{weight_id:int}/usun", dependencies=[Depends(verify_csrf)])
def delete_weight(weight_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = body.get_weight(db, user, weight_id)
    if w is None:
        raise HTTPException(404, "Nie ma takiego wpisu")
    db.delete(w)
    db.commit()
    return _go("/forma/sylwetka#wpisy")


@router.post("/sylwetka/ustawienia", dependencies=[Depends(verify_csrf)])
async def settings(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    p = body.prefs(db, user)
    try:
        p.goal_kg = body.parse_number(form.get("goal"), "Cel", *body.KG_RANGE, "kg")
    except body.BodyError as exc:
        db.rollback()
        return _page(request, db, user, error=str(exc), status_code=422)
    every = int(str(form.get("every") or p.check_every_days))
    if every in body.CHECK_EVERY_CHOICES:
        p.check_every_days = every
    p.reminders = bool(form.get("reminders"))
    db.commit()
    return _go("/forma/sylwetka#ustawienia")


# ---------------------------------------------------------------- measurements


def _check(db: Session, user: User, check_id: int) -> BodyCheck:
    c = body.get_check(db, user, check_id)
    if c is None:  # someone else's is the same as none
        raise HTTPException(404, "Nie ma takiego pomiaru")
    return c


def _check_page(request: Request, db: Session, user: User, c: BodyCheck | None, v: dict, error: str = ""):
    t = today()
    last = next((x for x in body.checks(db, user) if c is None or x.id != c.id), None)
    s = body.summary(db, user, t)
    return render(
        request,
        "body_check.html",
        {
            "user": user,
            "c": c,
            "v": v,
            "last": last,
            "error": error,
            "sites": body.SITES,
            "poses": body.POSES,
            "photo_of": body.photo_of,
            "bfmt": body.fmt,
            "today": t,
            "weighed_today": s.today_kg,
        },
        status_code=422 if error else 200,
    )


def _values(c: BodyCheck | None) -> dict:
    v = {k: body.fmt(getattr(c, k)) if c else "" for k in body.SITE_KEYS}
    v["day"] = (c.day if c else today()).isoformat()
    v["kg"] = ""
    return v


@router.get("/sylwetka/pomiar/nowy")
def new_check_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _check_page(request, db, user, None, _values(None))


@router.get("/sylwetka/pomiar/{check_id:int}")
def check_page(check_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = _check(db, user, check_id)
    return _check_page(request, db, user, c, _values(c))


async def _save(request: Request, db: Session, user: User, c: BodyCheck | None):
    form = await request.form()
    typed = {k: str(form.get(k) or "") for k in [*body.SITE_KEYS, "day", "kg"]}
    try:
        day = _day(form.get("day"))
        sizes = {key: body.parse_number(form.get(key), label, *body.CM_RANGE, "cm") for key, label, _ in body.SITES}
        kg = body.parse_number(form.get("kg"), "Waga", *body.KG_RANGE, "kg")
        photos = {}
        for pose, _ in body.POSES:
            f = form.get("photo_" + pose)
            if isinstance(f, UploadFile) and f.filename:
                photos[pose] = (f.filename, f.file)
        remove = {pose for pose, _ in body.POSES if form.get("remove_" + pose)}
        saved = body.save_check(db, user, c, day, sizes, photos, remove)
        if kg is not None:
            body.add_weight(db, user, day, kg)
    except body.BodyError as exc:
        db.rollback()
        return _check_page(request, db, user, c, typed, str(exc))
    db.commit()
    return _go(f"/forma/sylwetka#pomiar-{saved.id}")


@router.post("/sylwetka/pomiar", dependencies=[Depends(verify_csrf)])
async def add_check(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return await _save(request, db, user, None)


@router.post("/sylwetka/pomiar/{check_id:int}", dependencies=[Depends(verify_csrf)])
async def edit_check(
    check_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return await _save(request, db, user, _check(db, user, check_id))


@router.post("/sylwetka/pomiar/{check_id:int}/usun", dependencies=[Depends(verify_csrf)])
def delete_check(check_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    body.delete_check(db, _check(db, user, check_id))
    db.commit()
    return _go("/forma/sylwetka#pomiary")


# ---------------------------------------------------------------- photos


@router.get("/sylwetka/zdjecie/{photo_id:int}")
def photo(photo_id: int, maly: bool = False, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = body.get_photo(db, user, photo_id)
    if p is None:
        raise HTTPException(404, "Nie ma takiego zdjęcia")
    try:
        path = body.photo_path(p, thumb=maly)
    except FileNotFoundError as exc:
        raise HTTPException(404) from exc
    if not path.is_file():
        raise HTTPException(404, "Zdjęcie zniknęło z dysku – przywróć je z kopii zapasowej")
    return FileResponse(
        path,
        media_type="image/webp" if maly else p.content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        },
    )


@router.get("/sylwetka/porownaj")
def compare(
    request: Request,
    a: int = 0,
    b: int = 0,
    poza: str = "front",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Two measurements' photos of one pose side by side (default: the first and the latest)."""
    cs = [c for c in body.checks(db, user) if c.photos]
    if poza not in dict(body.POSES):
        poza = "front"
    by_id = {c.id: c for c in cs}
    left = by_id.get(a) or (cs[-1] if cs else None)
    right = by_id.get(b) or (cs[0] if cs else None)
    return render(
        request,
        "body_compare.html",
        {
            "user": user,
            "checks": cs,
            "left": left,
            "right": right,
            "poza": poza,
            "pose_names": dict(body.POSES),
            "poses": body.POSES,
            "photo_of": body.photo_of,
            "sites": body.SITES,
            "bfmt": body.fmt,
        },
    )
