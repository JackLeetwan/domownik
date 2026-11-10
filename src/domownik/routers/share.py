"""📤 Udostępnij → Domownik: what Android's share sheet sends to the installed app (Web Share Target).

Files (a photo, a PDF of an invoice) go straight to 📥 Do rozpoznania, like a quick photo. A link or a
text gets a page that asks where it goes – a recipe, the shopping list, a to-do, a note – and nothing is
saved before that tap.

The share comes as a POST without our CSRF token (the system makes it, not our page). It is still safe:
the session cookie is SameSite=Lax, so another site's form never arrives signed in, a foreign Origin is
refused, and a text is only shown – every save after it goes through the usual CSRF-checked forms.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from domownik import documents, recipe_link
from domownik.config import get_settings
from domownik.db import get_db
from domownik.models import User
from domownik.security import current_user
from domownik.web import render

router = APIRouter(prefix="/udostepnij", tags=["share"])

MAX_TEXT = 4000


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin or origin == "null":  # the share sheet: no page started it
        return True
    ours = {urlsplit(get_settings().base_url).netloc, request.url.netloc}
    return urlsplit(origin).netloc in ours


def _clean(v) -> str:
    return str(v or "").replace("\r\n", "\n").strip()[:MAX_TEXT]


def shared_text(title: str, text: str, url: str) -> tuple[str, str | None]:
    """What came in → (the text without the link, the link). Apps put the link in 'text' or in 'url'."""
    link = None
    for candidate in (url, text, title):
        try:
            link = recipe_link.find_url(candidate)
            break
        except recipe_link.RecipeLinkError:
            continue
    body = text.replace(link, "").strip() if link else text
    if title and title not in body and title != link:
        body = f"{title}\n{body}".strip()
    return body.strip(" \n–-:"), link


@router.get("")
def nothing(user: User = Depends(current_user)):
    """Opened by hand – nothing was shared."""
    return RedirectResponse("/", status_code=303)


@router.post("")
async def receive(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not _same_origin(request):
        raise HTTPException(403, "Udostępnianie działa tylko z telefonu")
    form = await request.form()
    files = documents.uploads_in(form)
    if files:
        try:
            created = documents.store_files(db, None, [(f.filename, f.file) for f in files], user=user)
        except documents.UploadError as exc:
            db.rollback()
            return render(request, "share.html", {"user": user, "error": str(exc)}, status_code=400)
        for att in created:
            att.inbox = True
        db.commit()
        return RedirectResponse("/zdjecia", status_code=303)

    body, link = shared_text(_clean(form.get("title")), _clean(form.get("text")), _clean(form.get("url")))
    if not body and not link:
        return RedirectResponse("/", status_code=303)
    lines = [ln.strip(" -•*\t") for ln in body.splitlines() if ln.strip(" -•*\t")]
    return render(
        request,
        "share.html",
        {
            "user": user,
            "body": body,
            "link": link,
            "first_line": (lines[0] if lines else (link or ""))[:200],
            "as_list": len(lines) > 1 and all(len(ln) <= 40 for ln in lines),  # 'mleko / chleb / masło'
        },
    )
