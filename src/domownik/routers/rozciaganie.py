"""🤸 Stretching in "Forma": the saved routines (shared, edited like the strength plans), the library and the
session with the timer (▶ Start of a routine). Sessions are private – another person's id is a 404.

The timer runs in the browser (app.js, "stretching session"): the server only hands it the routine and stores
the result at the end.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from domownik import stretching as st
from domownik import together
from domownik import training as tr
from domownik.db import get_db
from domownik.models import Stretch, StretchGroup, StretchRoutine, StretchRoutineItem, StretchSession, User
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/forma/rozciaganie")

KINDS = {"before": "Przed treningiem", "after": "Po treningu", "own": "Własny"}


def _go(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _routine(db: Session, routine_id: int) -> StretchRoutine:
    r = db.get(StretchRoutine, routine_id)
    if r is None or r.archived:
        raise HTTPException(404, "Nie ma takiego zestawu")
    return r


def _stretch(db: Session, stretch_id: int) -> Stretch:
    s = db.get(Stretch, stretch_id)
    if s is None:
        raise HTTPException(404, "Nie ma takiego ćwiczenia")
    return s


def _ctx(user: User, **kw) -> dict:
    return {"user": user, "st": st, "kinds": KINDS} | kw


@router.get("")
def routines_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    st.prepare(db)
    db.commit()
    return render(request, "stretch_routines.html", _ctx(user, routines=st.routines(db)))


@router.post("", dependencies=[Depends(verify_csrf)])
async def new_routine(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    name = str(form.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Podaj nazwę zestawu")
    top = max((r.position for r in st.routines(db)), default=-1)
    r = StretchRoutine(name=name, kind="own", position=top + 1)
    db.add(r)
    db.commit()
    return _go(f"/forma/rozciaganie/{r.id}")


@router.get("/{routine_id:int}")
def routine_page(
    routine_id: int,
    request: Request,
    edycja: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """A preview with ▶ Start; the editing (seconds, order, adding) only after "✏️ Edytuj" – or while it is empty."""
    r = _routine(db, routine_id)
    return render(request, "stretch_routine.html", _ctx(user, r=r, edit=bool(edycja) or not r.items))


@router.post("/{routine_id:int}", dependencies=[Depends(verify_csrf)])
async def save_routine(
    routine_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """The whole routine in one form: name, before / after / own, and every stretch's seconds."""
    r = _routine(db, routine_id)
    form = await request.form()
    r.name = str(form.get("name") or r.name).strip()[:60] or r.name
    kind = str(form.get("kind") or r.kind)
    if kind not in KINDS:
        raise ValueError("Nieznany rodzaj zestawu")
    r.kind = kind
    for it in r.items:
        raw = str(form.get(f"i{it.id}_seconds") or "").strip()
        if not raw:
            it.seconds = None
            continue
        v = int(raw)
        if not 5 <= v <= 600:
            raise ValueError("Czas ćwiczenia: 5–600 sekund")
        it.seconds = None if v == it.stretch.seconds else v
    db.commit()
    return _go(f"/forma/rozciaganie/{r.id}")


@router.post("/{routine_id:int}/cw/{item_id:int}/{action}", dependencies=[Depends(verify_csrf)])
def item_action(
    routine_id: int, item_id: int, action: str, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    r = _routine(db, routine_id)
    it = db.get(StretchRoutineItem, item_id)
    if it is None or it.routine_id != r.id or action not in ("gora", "dol", "usun"):
        raise HTTPException(404, "Nie ma takiej pozycji")
    items = list(r.items)
    i = items.index(it)
    if action == "usun":
        r.items.remove(it)
    elif action == "gora" and i > 0:
        items[i - 1], items[i] = items[i], items[i - 1]
    elif action == "dol" and i < len(items) - 1:
        items[i + 1], items[i] = items[i], items[i + 1]
    for pos, x in enumerate(x for x in items if x in r.items):
        x.position = pos
    db.commit()
    return _go(f"/forma/rozciaganie/{r.id}?edycja=1#i{item_id}")


@router.get("/{routine_id:int}/dodaj")
def add_page(
    routine_id: int,
    request: Request,
    typ: str = "",
    partia: str = "",
    sprzet: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    r = _routine(db, routine_id)
    st.ensure_library(db)
    db.commit()
    type_ = typ if typ in st.catalog()["types"] else None
    free = sprzet == "0"
    areas = st.areas_with(db, type_, free)
    area = partia if partia in {k for k, _, _ in areas} else None
    base = f"/forma/rozciaganie/{r.id}/dodaj"
    return render(
        request,
        "stretch_pick.html",
        _ctx(
            user,
            title=f"Dodaj do: {r.name}",
            back_url=f"/forma/rozciaganie/{r.id}?edycja=1",
            base=base,
            sections=st.groups(db, type_, area, free),
            type_filter=type_,
            area_filter=area,
            areas=areas,
            free_filter=free,
            pick_url=_pick_url(base, {"typ": type_, "partia": area, "sprzet": "0" if free else None}),
            marked={it.stretch_id for it in r.items},
            mark_label="✓ w zestawie",
        ),
    )


def _pick_url(base: str, now: dict):
    def pick_url(**change) -> str:
        """The picker with one filter changed and the others kept (None drops it; "" – explicitly none)."""
        q = now | change
        query = "&".join(f"{k}={v}" for k, v in q.items() if v is not None)
        return f"{base}?{query}" if query else base

    return pick_url


@router.post("/{routine_id:int}/dodaj/{stretch_id:int}", dependencies=[Depends(verify_csrf)])
def add(routine_id: int, stretch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = _routine(db, routine_id)
    s = _stretch(db, stretch_id)
    top = max((i.position for i in r.items), default=-1)
    it = StretchRoutineItem(stretch=s, position=top + 1)
    r.items.append(it)
    db.commit()
    return _go(f"/forma/rozciaganie/{r.id}?edycja=1#i{it.id}")


@router.post("/{routine_id:int}/archiwum", dependencies=[Depends(verify_csrf)])
def archive(routine_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _routine(db, routine_id).archived = True
    db.commit()
    return _go("/forma/rozciaganie")


@router.get("/cw/{stretch_id:int}")
def stretch_guide(stretch_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """ⓘ How the stretch is done – in the modal (the timer keeps going), else as its own page."""
    s = _stretch(db, stretch_id)
    ctx = _ctx(user, ex=s, guide=st.guide(s), img_start=st.image_start(s), img_end=st.image_end(s))
    if request.headers.get("hx-target") == "modal-body":
        return render(request, "partials/stretch_guide_modal.html", ctx)
    return render(request, "stretch_guide.html", ctx)


# ---------------------------------------------------------------- the session


def _session(db: Session, user: User, session_id: int) -> StretchSession:
    s = st.get_own(db, user, session_id)
    if s is None:
        raise HTTPException(404, "Nie ma takiej sesji")
    return s


@router.post("/{routine_id:int}/start", dependencies=[Depends(verify_csrf)])
async def start(routine_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = _routine(db, routine_id)
    if not r.items:
        raise ValueError("Zestaw jest pusty – najpierw dodaj ćwiczenia")
    form = await request.form()
    workout = None
    if raw := str(form.get("workout_id") or "").strip():
        workout = tr.get_own(db, user, int(raw))
    s = st.start(db, user, r, workout)
    db.commit()
    return _go(f"/forma/rozciaganie/sesja/{s.id}")


@router.get("/sesja/{session_id:int}")
def session_page(session_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = _session(db, user, session_id)
    g = s.group
    live = g is not None and s.finished_at is None
    invited = set(g.invited or []) if g else set()
    return render(
        request,
        "stretch_session.html",
        _ctx(
            user,
            s=s,
            transition=st.TRANSITION,
            after=_after_url(db, user, s),
            group=g if live else None,
            group_json=_group_state(g, user) if live else None,
            can_invite=[u for u in together.partners(db, user) if u.id not in invited] if s.finished_at is None else [],
        ),
    )


def _group_state(g: StretchGroup, user: User) -> dict:
    """What a browser on the shared timer needs: the state, its rev, the server's clock, who else is in."""
    mine = st.member(g, user)
    return {
        "plan": [x["id"] for x in (mine.steps if mine else [])],  # changed by 🔄 – the timer reloads
        "rev": g.rev,
        "state": g.state or {},
        "now": int(time.time() * 1000),
        "with": [x.user.name for x in g.sessions if x.user_id != user.id and x.user is not None],
        "waiting": len(set(g.invited or []) - {x.user_id for x in g.sessions} - {user.id}),
    }


def _after_url(db: Session, user: User, s: StretchSession) -> str:
    """Where to go when done: back to the workout it was started from, else Forma."""
    if s.workout_id and (w := tr.get_own(db, user, s.workout_id)) is not None:
        return f"/forma/trening/{w.id}"
    return "/forma"


@router.get("/sesja/{session_id:int}/zamien")
def swap_page(
    session_id: int,
    request: Request,
    poz: int = 0,
    typ: str | None = None,
    partia: str | None = None,
    sprzet: str = "",
    stale: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """🔄 In the middle of stretching: another stretch in place of this one; at first the same kind and area."""
    s = _session(db, user, session_id)
    if s.finished_at is not None or not 0 <= poz < len(s.steps):
        return _go(f"/forma/rozciaganie/sesja/{s.id}")
    st.ensure_library(db)
    db.commit()
    cur = db.get(Stretch, s.steps[poz]["id"])
    type_ = (typ or None) if typ is not None else (cur.type if cur else None)
    type_ = type_ if type_ in st.catalog()["types"] else None
    free = sprzet == "0"
    areas = st.areas_with(db, type_, free)
    area = (partia or None) if partia is not None else (cur.area if cur else None)
    area = area if area in {k for k, _, _ in areas} else None
    routine = s.routine if s.routine is not None and not s.routine.archived else None
    base = f"/forma/rozciaganie/sesja/{s.id}/zamien"
    pick_url = _pick_url(
        base,
        {"poz": poz, "typ": type_ or "", "partia": area or "", "sprzet": "0" if free else None, "stale": stale or None},
    )
    return render(
        request,
        "stretch_pick.html",
        _ctx(
            user,
            title=f"Zamień: {s.steps[poz]['name']}",
            back_url=f"/forma/rozciaganie/sesja/{s.id}",
            base=base,
            hidden={"poz": poz, "stale": "1" if stale and routine else ""},
            sections=st.groups(db, type_, area, free),
            type_filter=type_,
            area_filter=area,
            areas=areas,
            free_filter=free,
            pick_url=pick_url,
            marked={x["id"] for x in s.steps},
            mark_label="✓ w tej sesji",
            current_id=cur.id if cur else None,
            permanent=bool(stale) and routine is not None,
            permanent_url=pick_url(stale=None if stale else "1") if routine else None,
            routine=routine,
            together=s.group is not None,
        ),
    )


@router.post("/sesja/{session_id:int}/zamien/{stretch_id:int}", dependencies=[Depends(verify_csrf)])
async def swap(
    session_id: int,
    stretch_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    s = _session(db, user, session_id)
    form = await request.form()
    st.swap(s, int(str(form.get("poz") or 0)), _stretch(db, stretch_id), permanent=bool(form.get("stale")))
    db.commit()
    return _go(f"/forma/rozciaganie/sesja/{s.id}")


@router.post("/sesja/{session_id:int}/koniec", dependencies=[Depends(verify_csrf)])
async def finish(session_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = _session(db, user, session_id)
    form = await request.form()
    if s.finished_at is None:
        st.finish(s, int(str(form.get("done") or 0)), int(str(form.get("seconds") or 0)))
        db.commit()
    return _go(f"/forma/rozciaganie/sesja/{s.id}")


@router.post("/sesja/{session_id:int}/usun", dependencies=[Depends(verify_csrf)])
def delete_session(session_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = _session(db, user, session_id)
    db.delete(s)
    db.commit()
    return _go("/forma")


# ---------------------------------------------------------------- 👫 together: one timer


@router.post("/sesja/{session_id:int}/zapros", dependencies=[Depends(verify_csrf)])
async def invite(session_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = _session(db, user, session_id)
    if s.finished_at is not None:
        raise ValueError("Ta sesja już się skończyła")
    form = await request.form()
    ids = {str(v) for v in form.getlist("razem")}
    invitees = [u for u in together.partners(db, user) if str(u.id) in ids]
    if invitees:
        st.invite(db, s, invitees)
        db.commit()
    return _go(f"/forma/rozciaganie/sesja/{s.id}")


def _group(db: Session, user: User, group_id: int) -> StretchGroup:
    g = db.get(StretchGroup, group_id)
    if g is None or (st.member(g, user) is None and user.id not in (g.invited or [])):
        raise HTTPException(404, "Nie ma takiego wspólnego rozciągania")
    return g


@router.get("/razem/{group_id:int}")
def join(group_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The link from the push: joins straight away and opens one's own session on the shared timer."""
    s = st.join(db, _group(db, user, group_id), user)
    db.commit()
    return _go(f"/forma/rozciaganie/sesja/{s.id}")


@router.get("/razem/{group_id:int}/stan")
def group_state(group_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Polled every ~2 s by the timer of everybody in."""
    g = _group(db, user, group_id)
    return JSONResponse(_group_state(g, user))


@router.post("/razem/{group_id:int}/stan", dependencies=[Depends(verify_csrf)])
async def set_group_state(
    group_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """A change from one browser (start, pause, skip, back, end). An outdated one gets the current state back."""
    g = _group(db, user, group_id)
    if st.member(g, user) is None:
        raise HTTPException(404, "Nie ma takiego wspólnego rozciągania")
    body = await request.json()
    ok = st.set_state(g, int(body.get("rev", -1)), dict(body.get("state") or {}))
    db.commit()
    return JSONResponse({"ok": ok} | _group_state(g, user))
