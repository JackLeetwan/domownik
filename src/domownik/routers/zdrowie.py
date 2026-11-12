"""🩺 Zdrowie – visits, health issues, medicines and medical records per household member (see health.py)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import documents, health, privacy, sizes, supplements
from domownik.config import get_settings, today
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import (
    DOC_KINDS,
    MEDICAL_DOC_KINDS,
    BodySize,
    FitSize,
    HealthLink,
    HealthReading,
    Issue,
    Item,
    Medication,
    Supplement,
    Task,
    User,
)
from domownik.routers.contacts import tel_href
from domownik.routers.documents import _form_error, too_big
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter(prefix="/zdrowie")


def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _date(value) -> date | None:
    value = str(value or "").strip()
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        raise HTTPException(400, "Nieprawidłowa data") from None


def _member(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None or not item.is_member:
        raise HTTPException(404, "Nie ma takiego domownika")
    return item


def _issue(db: Session, issue_id: int) -> Issue:
    issue = db.get(Issue, issue_id)
    if issue is None or not issue.item.is_member:
        raise HTTPException(404, "Nie ma takiej sprawy")
    return issue


def _issue_of(db: Session, item: Item, raw) -> Issue | None:
    """The issue picked in a form – only one of this person's."""
    raw = str(raw or "").strip()
    issue = db.get(Issue, int(raw)) if raw.isdigit() else None
    return issue if issue is not None and issue.item_id == item.id else None


def _private(form, user: User, issue: Issue | None) -> int | None:
    """🔒 from the form; anything under a private issue is private too."""
    if issue is not None and issue.private_to_id:
        return issue.private_to_id
    return user.id if form.get("private") else None


def _common(db: Session, user: User) -> dict:
    return {
        "user": user,
        "doc_kinds": DOC_KINDS,
        "medical_kinds": {k: DOC_KINDS[k] for k in MEDICAL_DOC_KINDS},
        "human_size": documents.human_size,
        "max_upload_mb": get_settings().max_upload_mb,
        "tel_href": tel_href,
        "private_field": privacy.FIELD,
    }


@router.get("")
def overview(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = today()
    tree = Tree.load(db)
    people = health.members(db, tree)
    ov = health.health_rows(db, tree, t, people)
    cards = []
    for p in people:
        issues = [i for i in p.issues if i.resolved_on is None]
        meds_now, _ = health.split_meds(health.medications(db, p.id), t)
        cards.append({"item": p, "rows": ov.rows_by_person[p.id], "issues": issues, "meds": meds_now})
    money = health.spending(db, tree, people, t.year)
    size_remind = sizes.reminders(db, people, user, t)
    for c in cards:
        c["size_remind"] = size_remind.get(c["item"].id)
    return render(
        request,
        "zdrowie.html",
        {
            **_common(db, user),
            "cards": cards,
            "other_rows": ov.other_rows,
            "calendar": health.calendar_visits(db, t),
            "money": money,
            "year": t.year,
        },
    )


@router.get("/{item_id}")
def person(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = today()
    item = _member(db, item_id)
    tree = Tree.load(db)
    people = health.members(db, tree)
    ov = health.health_rows(db, tree, t, people)
    meds_now, meds_past = health.split_meds(health.medications(db, item.id), t)
    files = health.medical_files(db, item)
    issues = list(item.issues)
    own_supps = [s for s in supplements.mine(db, user) if s.medication_id is None] if item.user_id == user.id else []
    reminders = {
        s.medication_id: s
        for s in db.scalars(
            select(Supplement).where(Supplement.user_id == user.id, Supplement.medication_id.is_not(None))
        )
    }
    money = health.spending(db, tree, people, t.year)
    return render(
        request,
        "zdrowie_osoba.html",
        {
            **_common(db, user),
            "item": item,
            "people": people,
            "rows": ov.rows_by_person.get(item.id, []),
            "open_issues": [i for i in issues if i.resolved_on is None],
            "closed_issues": [i for i in issues if i.resolved_on is not None],
            "meds_now": meds_now,
            "meds_past": meds_past,
            "photos": [a for a in files if a.has_thumb],
            "files": [a for a in files if not a.has_thumb],
            "links": health.links(db, item.id),
            "doctors": [c for c in item.contacts if (c.role or "").startswith("Lekarz")],
            "other_contacts": [c for c in item.contacts if not (c.role or "").startswith("Lekarz")],
            "supplements": own_supps,
            "issue_names": {i.id: i.title for i in issues},
            "reminders": reminders,
            "profile": health.profile(item),
            "profile_fields": health.PROFILE,
            "blood_types": health.BLOOD_TYPES,
            "series": health.readings(db, item.id),
            "reading_kinds": health.READING_KINDS,
            "spent": money["by_person"].get(item.id, 0),
            "spent_rows": money["recent"].get(item.id, [])[:10],
            "money_found": money["found"],
            "year": t.year,
            "sz": sizes.load(db, item, user),
            "size_fields": sizes.FIELDS,
            "size_what": sizes.WHAT,
            "fmt_cm": sizes.fmt,
            "fit_label": sizes.fit_label,
        },
    )


@router.get("/sprawa/{issue_id}")
def issue_page(request: Request, issue_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = today()
    issue = _issue(db, issue_id)
    item = issue.item
    meds = [m for m in health.medications(db, item.id) if m.issue_id == issue.id]
    meds_now, meds_past = health.split_meds(meds, t)
    files = [a for a in item.attachments if a.issue_id == issue.id and documents.visible(db, a)]
    tree = Tree.load(db)
    return render(
        request,
        "zdrowie_sprawa.html",
        {
            **_common(db, user),
            "item": item,
            "issue": issue,
            "meds_now": meds_now,
            "meds_past": meds_past,
            "photos": [a for a in files if a.has_thumb],
            "files": [a for a in files if not a.has_thumb],
            "links": health.links(db, item.id, issue.id),
            "tasks": [x for x in health.issue_tasks(db, issue.id) if x.active],
            "done_tasks": [x for x in health.issue_tasks(db, issue.id) if not x.active],
            "label": tree.label,
            "reminders": {
                s.medication_id: s
                for s in db.scalars(
                    select(Supplement).where(Supplement.user_id == user.id, Supplement.medication_id.is_not(None))
                )
            },
        },
    )


# ---------------------------------------------------------------- issues (🔒 + back to Zdrowie)


@router.post("/{item_id}/sprawy", dependencies=[Depends(verify_csrf)])
async def add_issue(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _member(db, item_id)
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Podaj, co się dzieje")
    issue = Issue(
        item_id=item.id,
        title=title[:200],
        started_on=_date(form.get("started_on")) or today(),
        notes=(form.get("notes") or "").strip() or None,
        created_by_id=user.id,
        private_to_id=user.id if form.get("private") else None,
    )
    db.add(issue)
    db.commit()
    return _redirect(f"/zdrowie/sprawa/{issue.id}")


@router.post("/sprawa/{issue_id}/prywatna", dependencies=[Depends(verify_csrf)])
async def issue_private(
    request: Request, issue_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """🔒 on / off – only one's own issue can be shared again (others never see a private one anyway)."""
    issue = _issue(db, issue_id)
    form = await request.form()
    issue.private_to_id = user.id if form.get("private") else None
    for t in health.issue_tasks(db, issue.id):  # its visits / reminders go with it
        if t.assignee_id in (None, user.id) and t.private_to_id in (None, user.id):
            t.private_to_id = issue.private_to_id
    db.commit()
    return _redirect(f"/zdrowie/sprawa/{issue.id}")


@router.post("/sprawa/{issue_id}/zadanie", dependencies=[Depends(verify_csrf)])
async def add_issue_task(
    request: Request, issue_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """A one-off task of the issue: the follow-up visit, removing the stitches, checking the insurer's decision."""
    issue = _issue(db, issue_id)
    form = await request.form()
    title = (form.get("title") or "").strip()
    on = _date(form.get("on"))
    if not title or on is None:
        raise HTTPException(400, "Podaj, co i kiedy")
    db.add(
        Task(
            item_id=issue.item_id,
            issue_id=issue.id,
            title=title[:200],
            category="health",
            recurrence={"mode": "once", "date": on.isoformat()},
            lead_days=int(form.get("lead_days") or 2),
            next_due_date=on,
            direction="none",
            notes=(form.get("notes") or "").strip() or None,
            private_to_id=issue.private_to_id,
        )
    )
    db.commit()
    return _redirect(f"/zdrowie/sprawa/{issue.id}")


# ---------------------------------------------------------------- medicines


def _apply_med(m: Medication, form) -> None:
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Podaj nazwę leku")
    m.name = name[:120]
    m.dose = (form.get("dose") or "").strip()[:80] or None
    m.schedule = (form.get("schedule") or "").strip()[:120] or None
    m.started_on = _date(form.get("started_on"))
    m.ends_on = _date(form.get("ends_on"))
    if m.started_on and m.ends_on and m.ends_on < m.started_on:
        raise HTTPException(400, "Koniec jest przed początkiem")
    m.prescription = (form.get("prescription") or "").strip()[:200] or None
    m.rx_until = _date(form.get("rx_until"))
    m.notes = (form.get("notes") or "").strip() or None


def _after_med(db: Session, m: Medication, form, user: User) -> None:
    """Prescription task and "🔔 Przypominaj mi" (only the viewer's own reminder) from the form."""
    health.sync_rx_task(db, m)
    if "remind_field" in form:
        time = supplements.parse_time(form.get("remind_time")) if form.get("remind") else None
        if form.get("remind") and time is None:
            raise HTTPException(400, "Podaj godzinę przypomnienia, np. 8:00")
        health.set_reminder(db, m, user, time, today())


def _drop_med_extras(db: Session, m: Medication) -> None:
    """The medicine is over: its reminders stop, an open renewal task goes."""
    for s in db.scalars(select(Supplement).where(Supplement.medication_id == m.id)):
        s.active = False
    if m.rx_task_id:
        t = db.get(Task, m.rx_task_id)
        if t is not None and t.active:
            db.delete(t)
        m.rx_task_id = None


def _get_med(db: Session, med_id: int) -> Medication:
    m = db.get(Medication, med_id)
    if m is None or not health.issue_visible(db, m.issue_id):
        raise HTTPException(404, "Nie ma takiego leku")
    return m


def _back(form, fallback: str) -> RedirectResponse:
    nxt = str(form.get("next") or "")
    return _redirect(nxt if nxt.startswith("/zdrowie") else fallback)


@router.post("/{item_id}/leki", dependencies=[Depends(verify_csrf)])
async def add_med(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _member(db, item_id)
    form = await request.form()
    issue = _issue_of(db, item, form.get("issue_id"))
    m = Medication(item_id=item.id, issue_id=issue.id if issue else None, created_by_id=user.id)
    _apply_med(m, form)
    if m.started_on is None:
        m.started_on = today()
    m.private_to_id = _private(form, user, issue)
    db.add(m)
    db.flush()
    _after_med(db, m, form, user)
    db.commit()
    return _back(form, f"/zdrowie/{item.id}#leki")


@router.post("/leki/{med_id}/edit", dependencies=[Depends(verify_csrf)])
async def edit_med(request: Request, med_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = _get_med(db, med_id)
    form = await request.form()
    _apply_med(m, form)
    issue = _issue_of(db, m.item, form.get("issue_id"))
    m.issue_id = issue.id if issue else None
    if privacy.FIELD in form:
        m.private_to_id = _private(form, user, issue)
    _after_med(db, m, form, user)
    db.commit()
    return _back(form, f"/zdrowie/{m.item_id}#leki")


@router.post("/leki/{med_id}/koniec", dependencies=[Depends(verify_csrf)])
async def end_med(request: Request, med_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = _get_med(db, med_id)
    form = await request.form()
    yesterday = date.fromordinal(today().toordinal() - 1)  # "przestałem" – not taken any more from today
    m.ends_on = yesterday
    if m.started_on and m.started_on > yesterday:
        m.started_on = yesterday
    _drop_med_extras(db, m)
    db.commit()
    return _back(form, f"/zdrowie/{m.item_id}#leki")


@router.post("/leki/{med_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_med(request: Request, med_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = _get_med(db, med_id)
    form = await request.form()
    item_id = m.item_id
    _drop_med_extras(db, m)
    for s in db.scalars(select(Supplement).where(Supplement.medication_id == m.id)):
        db.delete(s)
    db.delete(m)
    db.commit()
    return _back(form, f"/zdrowie/{item_id}#leki")


# ---------------------------------------------------------------- records: files and links


@router.post("/{item_id}/pliki", dependencies=[Depends(verify_csrf)])
async def upload(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _member(db, item_id)
    target = "#health-files-error"
    if too_big(request):
        return _form_error(request, "Za dużo danych naraz – dodaj pliki w kilku partiach.", target)
    form = await request.form()
    issue = _issue_of(db, item, form.get("issue_id"))
    kind = str(form.get("doc_kind") or "")
    try:
        documents.store_files(
            db,
            item,
            [(f.filename or "", f.file) for f in documents.uploads_in(form)],
            user=user,
            doc_kind=kind if kind in DOC_KINDS else "results",
            description=form.get("description") or None,
            issue_id=issue.id if issue else None,
            private_to_id=_private(form, user, issue),
        )
    except documents.UploadError as exc:
        db.rollback()
        return _form_error(request, str(exc), target)
    db.commit()
    return _back(form, f"/zdrowie/{item.id}#dokumentacja")


@router.post("/{item_id}/linki", dependencies=[Depends(verify_csrf)])
async def add_link(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _member(db, item_id)
    form = await request.form()
    url = health.clean_url(form.get("url"))
    if url is None:
        raise HTTPException(400, "Podaj adres strony (https://…)")
    issue = _issue_of(db, item, form.get("issue_id"))
    title = (form.get("title") or "").strip() or url.split("//", 1)[-1].split("/", 1)[0]
    db.add(
        HealthLink(
            item_id=item.id,
            issue_id=issue.id if issue else None,
            title=title[:200],
            url=url,
            private_to_id=_private(form, user, issue),
            created_by_id=user.id,
        )
    )
    db.commit()
    return _back(form, f"/zdrowie/{item.id}#dokumentacja")


@router.post("/linki/{link_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_link(
    request: Request, link_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    h = db.get(HealthLink, link_id)
    if h is None or not health.issue_visible(db, h.issue_id):
        raise HTTPException(404, "Nie ma takiego linku")
    form = await request.form()
    item_id = h.item_id
    db.delete(h)
    db.commit()
    return _back(form, f"/zdrowie/{item_id}#dokumentacja")


# ---------------------------------------------------------------- medical profile and readings


@router.post("/{item_id}/profil", dependencies=[Depends(verify_csrf)])
async def save_profile(
    request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    item = _member(db, item_id)
    health.save_profile(item, await request.form())
    db.commit()
    return _redirect(f"/zdrowie/{item.id}#profil")


@router.post("/{item_id}/pomiary", dependencies=[Depends(verify_csrf)])
async def add_reading(
    request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    item = _member(db, item_id)
    form = await request.form()
    name = (form.get("name") or "").strip()[:60]
    value = health.parse_number(form.get("value"))
    if not name or value is None:
        raise HTTPException(400, "Podaj, co zmierzone i wynik")
    kind = health.READING_KINDS.get(name)
    value2 = health.parse_number(form.get("value2")) if kind is None or kind[1] else None
    if kind and kind[1] and value2 is None:
        raise HTTPException(400, "Ciśnienie: podaj obie wartości, np. 120 / 80")
    unit = (form.get("unit") or "").strip()[:20] or (kind[0] if kind else None)
    db.add(
        HealthReading(
            item_id=item.id,
            name=name,
            value=value,
            value2=value2,
            unit=unit,
            measured_on=_date(form.get("measured_on")) or today(),
            note=(form.get("note") or "").strip()[:200] or None,
            private_to_id=user.id if form.get("private") else None,
            created_by_id=user.id,
        )
    )
    db.commit()
    return _redirect(f"/zdrowie/{item.id}#pomiary")


@router.post("/pomiary/{reading_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_reading(reading_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = db.get(HealthReading, reading_id)
    if r is None:
        raise HTTPException(404, "Nie ma takiego pomiaru")
    item_id = r.item_id
    db.delete(r)
    db.commit()
    return _redirect(f"/zdrowie/{item_id}#pomiary")


# ---------------------------------------------------------------- 📏 Wymiary (sizes.py)


@router.post("/{item_id}/wymiary", dependencies=[Depends(verify_csrf)])
async def add_sizes(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _member(db, item_id)
    form = await request.form()
    values = {k: sizes.parse_cm(form.get(k)) for k in sizes.KEYS}
    if all(v is None for v in values.values()):
        raise HTTPException(400, "Wpisz choć jeden wymiar w cm, np. 27,5")
    db.add(
        BodySize(
            item_id=item.id,
            measured_on=_date(form.get("measured_on")) or today(),
            **values,
            note=(form.get("note") or "").strip()[:200] or None,
            private_to_id=user.id if form.get("private") else None,
            created_by_id=user.id,
        )
    )
    db.commit()
    return _redirect(f"/zdrowie/{item.id}#wymiary")


@router.post("/wymiary/{entry_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_sizes(entry_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    e = db.get(BodySize, entry_id)
    if e is None or e.private_to_id not in (None, user.id):
        raise HTTPException(404, "Nie ma takiego pomiaru")
    item_id = e.item_id
    db.delete(e)
    db.commit()
    return _redirect(f"/zdrowie/{item_id}#wymiary")


@router.post("/{item_id}/rozmiary", dependencies=[Depends(verify_csrf)])
async def add_fit(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _member(db, item_id)
    form = await request.form()
    what = (form.get("what") or "").strip()[:40]
    size = (form.get("size") or "").strip()[:30]
    if not what or not size:
        raise HTTPException(400, "Podaj, co i jaki rozmiar")
    db.add(
        FitSize(
            item_id=item.id,
            what=what,
            brand=(form.get("brand") or "").strip()[:60] or None,
            size=size,
            note=(form.get("note") or "").strip()[:200] or None,
        )
    )
    db.commit()
    return _redirect(f"/zdrowie/{item.id}#wymiary")


@router.post("/rozmiary/{fit_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_fit(fit_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    f = db.get(FitSize, fit_id)
    if f is None:
        raise HTTPException(404, "Nie ma takiego rozmiaru")
    item_id = f.item_id
    db.delete(f)
    db.commit()
    return _redirect(f"/zdrowie/{item_id}#wymiary")
