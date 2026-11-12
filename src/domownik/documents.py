"""Files kept with items (photos, PDFs, invoices…) and warranties / contracts with an end date.

Files live in data/uploads/files/<item_id>/<random>.<ext> (uploads are already in backups and
restores); the database keeps only metadata. The type is decided by the file's first bytes,
never by the name the browser sent.
"""

from __future__ import annotations

import calendar
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from domownik.config import get_settings
from domownik.models import DOC_KINDS, Attachment, BoardNote, Completion, Issue, Item, Task, User, Warranty

log = logging.getLogger("domownik")

THUMB_PX = 480
CHUNK = 1024 * 1024
STORAGE_RE = re.compile(r"files/\d+/[0-9a-f]{32}\.[a-z0-9]{2,5}")
REMINDER_LEAD_DAYS = 30  # warranty / contract end shows up a month ahead
WARRANTY_SOON_DAYS = 30

ALLOWED_HINT = "zdjęcia (JPG, PNG, WebP, HEIC), PDF, dokumenty Office/OpenDocument, TXT/CSV, ZIP"


class UploadError(ValueError):
    """Shown to the user as is (Polish)."""


@dataclass(frozen=True)
class FileType:
    ext: str
    mime: str
    kind: str  # image (Pillow can make a thumbnail) | heic | pdf | file

    @property
    def inline(self) -> bool:
        """Opened in the browser tab (photo, PDF); everything else is downloaded."""
        return self.kind in ("image", "heic", "pdf")


_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_ZIP_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "zip": "application/zip",
}
_OLE_TYPES = {"doc": "application/msword", "xls": "application/vnd.ms-excel", "ppt": "application/vnd.ms-powerpoint"}
_TEXT_TYPES = {"txt": "text/plain", "csv": "text/csv"}


def _ext(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower().lstrip(".")


def _is_text(head: bytes) -> bool:
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        if exc.start >= len(head) - 4:
            return True  # a multi-byte character cut at the end of the sample
    try:
        head.decode("cp1250")  # Polish Windows text
        return True
    except UnicodeDecodeError:
        return False


def sniff(head: bytes, filename: str) -> FileType:
    """What the file really is, from its first bytes (the name only picks among container formats)."""
    ext = _ext(filename)
    if head.startswith(b"\xff\xd8\xff"):
        return FileType("jpg", "image/jpeg", "image")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileType("png", "image/png", "image")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return FileType("webp", "image/webp", "image")
    if head.startswith((b"GIF87a", b"GIF89a")):
        return FileType("gif", "image/gif", "image")
    if head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        return FileType("heic", "image/heic", "heic")
    if head.lstrip()[:5] == b"%PDF-":
        return FileType("pdf", "application/pdf", "pdf")
    if head.startswith(b"PK\x03\x04") and ext in _ZIP_TYPES:
        return FileType(ext, _ZIP_TYPES[ext], "file")
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and ext in _OLE_TYPES:
        return FileType(ext, _OLE_TYPES[ext], "file")
    if ext in _TEXT_TYPES and b"\x00" not in head and _is_text(head):
        return FileType(ext, _TEXT_TYPES[ext], "file")
    raise UploadError(f"„{filename}”: nieobsługiwany typ pliku. Można dodać: {ALLOWED_HINT}.")


def clean_name(name: str | None) -> str:
    """The name the file is shown and downloaded under: no directories, no control characters."""
    name = (name or "").replace("\\", "/")
    name = PurePosixPath(name).name
    name = "".join(ch for ch in unicodedata.normalize("NFC", name) if unicodedata.category(ch)[0] != "C")
    name = name.strip().strip(".") or "plik"
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 190 - len(ext)] + dot + ext) if dot and len(ext) <= 8 else name[:200]
    return name


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB".replace(".", ",")


# ---------------------------------------------------------------- paths


def visible(db: Session, att: Attachment | None) -> bool:
    """A file of someone else's private note / task (or of ticking such a task off) is as hidden as its owner:
    db.get() of a private row returns None for anyone else."""
    if att is None:
        return False
    if att.note_id is not None and db.get(BoardNote, att.note_id) is None:
        return False
    if att.task_id is not None and db.get(Task, att.task_id) is None:
        return False
    if att.issue_id is not None and db.get(Issue, att.issue_id) is None:
        return False
    if att.completion_id is not None:
        comp = db.get(Completion, att.completion_id)
        if comp is not None and db.get(Task, comp.task_id) is None:
            return False
    return True


def file_path(att: Attachment) -> Path:
    """Absolute path of the stored file; refuses anything that is not one of our generated names."""
    if not STORAGE_RE.fullmatch(att.storage_path or ""):
        raise FileNotFoundError(att.storage_path)
    root = get_settings().uploads_dir.resolve()
    path = (root / att.storage_path).resolve()
    if not path.is_relative_to(root):
        raise FileNotFoundError(att.storage_path)
    return path


def thumb_path(att: Attachment) -> Path:
    path = file_path(att)
    return path.with_name(path.stem + ".thumb.webp")


def _make_thumb(src: Path, dst: Path) -> bool:
    """Small WebP preview, turned upright the way the phone held it (EXIF orientation)."""
    try:
        with Image.open(src) as im:
            im.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))  # JPEG: decode at a fraction of the size
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_PX, THUMB_PX))
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if im.mode in ("LA", "PA") or "transparency" in im.info else "RGB")
            im.save(dst, "WEBP", quality=78)
        return True
    except Exception:  # a preview is a nicety – the file itself is kept
        log.exception("thumbnail failed for %s", src.name)
        dst.unlink(missing_ok=True)
        return False


def _check_image(path: Path, name: str) -> None:
    try:
        with Image.open(path) as im:
            im.verify()
    except Image.DecompressionBombError as exc:
        raise UploadError(f"„{name}”: zdjęcie ma zbyt dużą rozdzielczość.") from exc
    except Exception as exc:
        raise UploadError(f"„{name}”: plik wygląda na uszkodzony – to nie jest poprawne zdjęcie.") from exc


GPS_IFD = 0x8825
_STRIP_FORMATS = {"JPEG": "JPEG", "MPO": "JPEG", "PNG": "PNG", "WEBP": "WEBP"}


def strip_location(path: Path) -> bool:
    """Remove where a photo was taken (EXIF GPS, XMP) – the files go to backups on a cloud drive.

    The rest of EXIF stays, orientation included, so the photo still shows upright. Rewritten only
    when there is something to remove (JPEG keeps its quantisation tables – no visible loss).
    Returns True when the file was rewritten.
    """
    with Image.open(path) as im:
        fmt = _STRIP_FORMATS.get(im.format or "")
        if fmt is None or (getattr(im, "n_frames", 1) > 1 and fmt != "JPEG"):  # animated: left as is
            return False
        exif = im.getexif()
        has_gps = GPS_IFD in exif
        has_xmp = any(k in im.info for k in ("xmp", "XML:com.adobe.xmp"))
        if not (has_gps or has_xmp):
            return False
        if has_gps:
            del exif[GPS_IFD]
        opts: dict = {"exif": exif.tobytes()}
        if im.info.get("icc_profile"):
            opts["icc_profile"] = im.info["icc_profile"]
        if fmt == "JPEG":
            opts |= {"quality": "keep", "subsampling": "keep", "progressive": bool(im.info.get("progressive"))}
        elif fmt == "WEBP":
            opts |= {"lossless": True} if im.info.get("lossless") else {"quality": 90}
        tmp = path.with_name(path.stem + ".tmp")
        try:
            im.save(tmp, fmt, **opts)
        except Exception:
            tmp.unlink(missing_ok=True)
            if fmt != "JPEG":
                raise
            opts |= {"quality": 92, "subsampling": 0}  # e.g. MPO frames refuse "keep"
            im.save(tmp, fmt, **opts)
    tmp.replace(path)
    return True


# ---------------------------------------------------------------- storing / removing


def uploads_in(form) -> list[UploadFile]:
    """The files picked in a form's "files" inputs (an empty input sends a part without a name)."""
    return [v for v in form.getlist("files") if isinstance(v, UploadFile) and v.filename]


def store_files(
    db: Session,
    item: Item | None,
    uploads: list[tuple[str, BinaryIO]],
    *,
    user: User | None,
    doc_kind: str | None = None,
    description: str | None = None,
    completion_id: int | None = None,
    note_id: int | None = None,
    recipe_id: int | None = None,
    issue_id: int | None = None,
    private_to_id: int | None = None,
) -> list[Attachment]:
    """Save uploaded files (name, file object) with the item – all of them or, on any error, none.

    item None: a note's photo (note_id) or a recipe's (recipe_id), kept in files/0/.

    Raises UploadError with a message for the user. The caller commits.
    """
    s = get_settings()
    limit = s.max_upload_mb * 1024 * 1024
    uploads = [(n, f) for n, f in uploads if n]
    if not uploads:
        raise UploadError("Wybierz plik albo zrób zdjęcie.")
    if len(uploads) > s.max_upload_files:
        raise UploadError(f"Naraz można dodać najwyżej {s.max_upload_files} plików.")
    kind = doc_kind if doc_kind in DOC_KINDS else None
    folder_id = item.id if item is not None else 0
    folder = s.files_dir / str(folder_id)
    written: list[Path] = []
    created: list[Attachment] = []
    try:
        for raw_name, fobj in uploads:
            name = clean_name(raw_name)
            fobj.seek(0)
            head = fobj.read(4096)
            if not head:
                raise UploadError(f"„{name}”: plik jest pusty.")
            ftype = sniff(head, name)
            folder.mkdir(parents=True, exist_ok=True)
            rel = f"files/{folder_id}/{secrets.token_hex(16)}.{ftype.ext}"
            dest = s.uploads_dir / rel
            size = 0
            fobj.seek(0)
            with open(dest, "xb") as out:
                written.append(dest)
                while chunk := fobj.read(CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise UploadError(f"„{name}”: plik jest za duży (najwyżej {s.max_upload_mb} MB).")
                    out.write(chunk)
            has_thumb = False
            if ftype.kind == "image":
                _check_image(dest, name)
                try:
                    if strip_location(dest):
                        size = dest.stat().st_size
                except Exception:  # an odd but valid photo is still kept
                    log.exception("could not strip location from %s", dest.name)
                    dest.with_name(dest.stem + ".tmp").unlink(missing_ok=True)
                thumb = dest.with_name(dest.stem + ".thumb.webp")
                has_thumb = _make_thumb(dest, thumb)
                if has_thumb:
                    written.append(thumb)
            att = Attachment(
                item_id=item.id if item is not None else None,
                note_id=note_id,
                recipe_id=recipe_id,
                issue_id=issue_id,
                private_to_id=private_to_id,
                completion_id=completion_id,
                storage_path=rel,
                original_name=name,
                content_type=ftype.mime,
                size=size,
                doc_kind=kind or ("photo" if ftype.kind in ("image", "heic") else "other"),
                description=(description or "").strip()[:300] or None,
                has_thumb=has_thumb,
                created_by_id=user.id if user else None,
            )
            db.add(att)
            created.append(att)
        db.flush()
    except BaseException:
        for p in written:
            p.unlink(missing_ok=True)
        for att in created:
            if att in db:
                db.expunge(att)
        raise
    return created


def delete_attachment(db: Session, att: Attachment) -> None:
    """Remove the row and the bytes (file + preview). The caller commits."""
    paths = []
    try:
        paths = [file_path(att), thumb_path(att)]
    except FileNotFoundError:
        log.warning("attachment %s has an unexpected path – removing only the row", att.id)
    db.delete(att)
    db.flush()
    for p in paths:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- warranties


def warranty_status(w: Warranty, today: date) -> tuple[str, int | None]:
    """(status, days left): ok / soon / today / expired / open (no end date)."""
    if w.ends_on is None:
        return "open", None
    days = (w.ends_on - today).days
    if days < 0:
        return "expired", days
    if days == 0:
        return "today", 0
    if days <= WARRANTY_SOON_DAYS:
        return "soon", days
    return "ok", days


def parse_loose_date(value: str | None) -> date | None:
    """'2029-05-31', '31.05.2029', '05.2029' (→ end of month), '2029' (→ 31 Dec) – for template attributes."""
    v = (value or "").strip()
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", v):
            return date(int(m[3]), int(m[2]), int(m[1]))
        if m := re.fullmatch(r"(\d{1,2})[./](\d{4})", v):
            y, mo = int(m[2]), int(m[1])
            return date(y, mo, calendar.monthrange(y, mo)[1])
        if m := re.fullmatch(r"(\d{4})", v):
            return date(int(m[1]), 12, 31)
    except ValueError:
        return None
    return None


def reminder_title(w: Warranty) -> str:
    return f"Koniec: {w.name}"[:200]


REMINDER_NOTE = (
    "Przypomnienie z gwarancji / umowy – datę końca zmienisz w sekcji „Gwarancje i umowy” na stronie elementu."
)


def sync_reminder(db: Session, w: Warranty, today: date) -> None:
    """Keep the one-off reminder task in step with the warranty's end date. The caller commits.

    No end date, or an end already in the past → no (active) reminder. A reminder ticked off
    ("Zrobione") stays done until the end date changes.
    """
    task = db.get(Task, w.task_id) if w.task_id else None
    if w.ends_on is None or w.ends_on < today:
        if task is not None and task.active:
            db.delete(task)
            w.task_id = None
        return
    when = w.ends_on.isoformat()
    if task is None:
        task = Task(item_id=w.item_id, lead_days=REMINDER_LEAD_DAYS, direction="none", notes=REMINDER_NOTE)
        db.add(task)
    elif not task.active and any(c.due_date == w.ends_on for c in task.completions):
        task.title = reminder_title(w)
        return  # already acknowledged for this end date
    task.item_id = w.item_id
    task.title = reminder_title(w)
    task.category = "warranty"
    task.recurrence = {"mode": "once", "date": when}
    task.next_due_date = w.ends_on
    task.active = True
    db.flush()
    w.task_id = task.id


def delete_warranty(db: Session, w: Warranty) -> None:
    """The warranty goes with its reminder (the attached document stays). The caller commits."""
    task = db.get(Task, w.task_id) if w.task_id else None
    db.delete(w)
    if task is not None:
        db.delete(task)
    db.flush()


def warranty_for_task(db: Session, task_id: int) -> Warranty | None:
    return db.scalar(select(Warranty).where(Warranty.task_id == task_id))
