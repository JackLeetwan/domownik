"""Documents and photos kept with items, and warranties / contracts with an end date."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from markupsafe import escape
from sqlalchemy.orm import Session

from domownik import documents
from domownik.config import get_settings, today
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import DOC_KINDS, Attachment, Item, User, Warranty
from domownik.security import current_user, verify_csrf
from domownik.web import is_htmx, render

router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _get_item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Nie ma takiego elementu")
    return item


def _get_attachment(db: Session, att_id: int) -> Attachment:
    att = db.get(Attachment, att_id)
    if not documents.visible(db, att):
        raise HTTPException(404, "Nie ma takiego pliku")
    return att


def _item_file(db: Session, att_id: int) -> Attachment:
    """Preview / edit / delete windows are for item documents; a note's or recipe's photo is handled there."""
    att = _get_attachment(db, att_id)
    if att.item_id is None:
        where = "przepisu" if att.recipe_id is not None else "notatki"
        raise HTTPException(404, f"To zdjęcie {where} – zarządzasz nim na stronie {where}")
    return att


def _get_warranty(db: Session, wid: int) -> Warranty:
    w = db.get(Warranty, wid)
    if w is None:
        raise HTTPException(404, "Nie ma takiej gwarancji")
    return w


def _form_error(request: Request, msg: str, target: str) -> Response:
    """Message under the form (htmx) or a plain error page."""
    if is_htmx(request):
        return HTMLResponse(
            f'<p class="error">{escape(msg)}</p>',
            status_code=422,
            headers={"HX-Retarget": target, "HX-Reswap": "innerHTML"},
        )
    return render(request, "error.html", {"code": 400, "detail": msg}, status_code=400)


def too_big(request: Request) -> bool:
    """Reject an oversized request before anything is stored (the per-file limit is checked while saving)."""
    s = get_settings()
    cl = request.headers.get("content-length") or ""
    return cl.isdigit() and int(cl) > (s.max_upload_mb * s.max_upload_files + 1) * 1024 * 1024


# ---------------------------------------------------------------- files


@router.post("/items/{item_id}/files", dependencies=[Depends(verify_csrf)])
async def upload(request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _get_item(db, item_id)
    target = "#files-error"
    if too_big(request):
        return _form_error(request, "Za dużo danych naraz – dodaj pliki w kilku partiach.", target)
    form = await request.form()
    files = documents.uploads_in(form)
    try:
        documents.store_files(
            db,
            item,
            [(f.filename or "", f.file) for f in files],
            user=user,
            doc_kind=form.get("doc_kind") or None,
            description=form.get("description") or None,
        )
    except documents.UploadError as exc:
        db.rollback()
        return _form_error(request, str(exc), target)
    db.commit()
    return _redirect(f"/items/{item.id}#dokumenty")


def _file_headers(extra: dict | None = None) -> dict:
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"}
    headers.update(extra or {})
    return headers


@router.get("/files/{att_id}")
def download(att_id: int, dl: bool = False, db: Session = Depends(get_db), user: User = Depends(current_user)):
    att = _get_attachment(db, att_id)
    try:
        path = documents.file_path(att)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Nie ma takiego pliku") from exc
    if not path.is_file():
        raise HTTPException(404, "Plik zniknął z dysku – przywróć go z kopii zapasowej")
    inline = not dl and (att.is_image or att.is_pdf)
    extra = {}
    if att.is_image:
        # a photo opened in its own tab can run nothing
        extra["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    return FileResponse(
        path,
        media_type=att.content_type if inline else "application/octet-stream",
        filename=att.original_name,
        content_disposition_type="inline" if inline else "attachment",
        headers=_file_headers(extra),
    )


@router.get("/files/{att_id}/thumb")
def thumbnail(att_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    att = _get_attachment(db, att_id)
    try:
        path = documents.thumb_path(att)
    except FileNotFoundError as exc:
        raise HTTPException(404) from exc
    if not att.has_thumb or not path.is_file():
        raise HTTPException(404, "Brak podglądu")
    return FileResponse(path, media_type="image/webp", headers=_file_headers())


@router.get("/files/{att_id}/view")
def view(request: Request, att_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Preview + edit window (htmx) or the same as a page of its own."""
    att = _item_file(db, att_id)
    page = not is_htmx(request) or request.headers.get("hx-boosted") == "true"
    tree = Tree.load(db, include_archived=True)
    return render(
        request,
        "doc_page.html" if page else "partials/doc_modal.html",
        {
            "user": user,
            "page": page,
            "a": att,
            "item": att.item,
            "path": tree.path(att.item_id) if page else [],
            "doc_kinds": DOC_KINDS,
            "human_size": documents.human_size,
        },
    )


@router.post("/files/{att_id}/edit", dependencies=[Depends(verify_csrf)])
async def edit_file(request: Request, att_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    att = _item_file(db, att_id)
    form = await request.form()
    if form.get("doc_kind") in DOC_KINDS:
        att.doc_kind = form["doc_kind"]
    att.description = (form.get("description") or "").strip()[:300] or None
    name = documents.clean_name(form.get("original_name") or att.original_name)
    att.original_name = name
    db.commit()
    return _redirect(f"/items/{att.item_id}#dokumenty")


@router.post("/files/{att_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_file(att_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    att = _item_file(db, att_id)
    item_id = att.item_id
    documents.delete_attachment(db, att)
    db.commit()
    return _redirect(f"/items/{item_id}#dokumenty")


# ---------------------------------------------------------------- warranties / contracts


def _parse_date(value) -> date | None:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def _apply_warranty_form(db: Session, w: Warranty, form, item_id: int) -> str | None:
    name = (form.get("name") or "").strip()
    if not name:
        return "Podaj nazwę, np. „Gwarancja producenta”."
    try:
        starts, ends = _parse_date(form.get("starts_on")), _parse_date(form.get("ends_on"))
    except ValueError:
        return "Nieprawidłowa data."
    if starts and ends and ends < starts:
        return "Data końca jest wcześniejsza niż początek."
    att_id = form.get("attachment_id") or ""
    att = db.get(Attachment, int(att_id)) if att_id.isdigit() else None
    if att is not None and att.item_id != item_id:
        att = None
    w.name = name[:200]
    w.starts_on, w.ends_on = starts, ends
    w.notes = (form.get("notes") or "").strip() or None
    w.attachment_id = att.id if att else None
    return None


@router.post("/items/{item_id}/warranties", dependencies=[Depends(verify_csrf)])
async def add_warranty(
    request: Request, item_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    item = _get_item(db, item_id)
    form = await request.form()
    w = Warranty(item_id=item.id, created_by_id=user.id)
    if err := _apply_warranty_form(db, w, form, item.id):
        return _form_error(request, err, "#warranty-error")
    db.add(w)
    db.flush()
    documents.sync_reminder(db, w, today())
    db.commit()
    return _redirect(f"/items/{item.id}#dokumenty")


@router.post("/warranties/{wid}/edit", dependencies=[Depends(verify_csrf)])
async def edit_warranty(request: Request, wid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _get_warranty(db, wid)
    form = await request.form()
    if err := _apply_warranty_form(db, w, form, w.item_id):
        return _form_error(request, err, f"#warranty-error-{w.id}")
    documents.sync_reminder(db, w, today())
    db.commit()
    return _redirect(f"/items/{w.item_id}#dokumenty")


@router.post("/warranties/{wid}/delete", dependencies=[Depends(verify_csrf)])
def delete_warranty(wid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _get_warranty(db, wid)
    item_id = w.item_id
    documents.delete_warranty(db, w)
    db.commit()
    return _redirect(f"/items/{item_id}#dokumenty")
