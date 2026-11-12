"""🩺 Zdrowie – visits, health issues, medicines and medical records per household member (see health.py)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from domownik import documents, health, privacy, supplements
from domownik.config import get_settings, today
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import DOC_KINDS, MEDICAL_DOC_KINDS, HealthLink, Issue, Item, Medication, Task, User
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
    return render(
        request,
        "zdrowie.html",
        {
            **_common(db, user),
            "cards": cards,
            "other_rows": ov.other_rows,
            "calendar": health.calendar_visits(db, t),
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
    own_supps = supplements.mine(db, user) if item.user_id == user.id else []
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
    m.notes = (form.get("notes") or "").strip() or None


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
    db.commit()
    return _back(form, f"/zdrowie/{m.item_id}#leki")


@router.post("/leki/{med_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_med(request: Request, med_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = _get_med(db, med_id)
    form = await request.form()
    item_id = m.item_id
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
