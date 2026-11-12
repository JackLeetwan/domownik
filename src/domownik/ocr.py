"""📝 Reading documents into text – the queue on the server, the work on the PC.

A photo or a PDF put into 📥 Do rozpoznania is queued here. The worker on the PC (deploy/pc/ocr_worker.py)
asks /api/ocr for the next job whenever it may use the graphics card – the PC is on, no game is running and
the local models were not switched off by hand – reads the file (the PDF's own text when it has one, otherwise
the image cut into strips the model reads at full resolution) and sends the text back. The server never waits
for the PC: a job simply stays queued until it is taken.

A taken job is leased: not answered within LEASE it goes back to the queue (the PC was switched off mid-way);
after MAX_ATTEMPTS it is marked failed and the photo is described by hand, as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from domownik.models import Attachment, OcrJob

LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3
MAX_TEXT = 200_000  # characters kept – a 10-page scan is ~30 000
READABLE = ("image/", "application/pdf")


def _now() -> datetime:
    """Naive UTC, like created_at (SQLite func.now())."""
    return datetime.now(UTC).replace(tzinfo=None)


def readable(att: Attachment) -> bool:
    return att.content_type.startswith(READABLE)


def enqueue(db: Session, att: Attachment, *, priority: int = 0) -> OcrJob | None:
    """Queue the attachment for reading (once). The caller commits."""
    if not readable(att):
        return None
    if att.ocr is not None:
        return att.ocr
    job = OcrJob(attachment=att, priority=priority, status="queued")
    db.add(job)
    return job


def _takeable(now: datetime):
    return or_(OcrJob.status == "queued", (OcrJob.status == "working") & (OcrJob.lease_until < now))


def claim(db: Session) -> OcrJob | None:
    """The next job for the worker: new photos first, then the oldest. The caller commits."""
    now = _now()
    while True:
        job = db.scalars(
            select(OcrJob).where(_takeable(now)).order_by(OcrJob.priority, OcrJob.created_at, OcrJob.id).limit(1)
        ).first()
        if job is None:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.lease_until = "failed", None
            job.error = job.error or "Nie udało się odczytać (komputer nie oddał wyniku)"
            continue
        job.status, job.attempts, job.lease_until = "working", job.attempts + 1, now + LEASE
        return job


def finish(
    db: Session,
    job: OcrJob,
    *,
    text: str | None,
    source: str | None,
    model: str | None,
    pages: int | None,
    error: str | None,
) -> None:
    """The worker's answer. An error is retried until MAX_ATTEMPTS, then the job fails. The caller commits."""
    job.lease_until = None
    if error:
        job.error = error[:300]
        job.status = "failed" if job.attempts >= MAX_ATTEMPTS else "queued"
        return
    job.status, job.error, job.done_at = "done", None, _now()
    job.text = (text or "").strip()[:MAX_TEXT]
    job.source = (source or "")[:20] or None
    job.model = (model or "")[:80] or None
    job.pages = pages


def release(db: Session, job: OcrJob) -> None:
    """The worker gave the job back unread (a game started, the PC is shutting down) – it waits again,
    and this attempt does not count."""
    job.status, job.lease_until, job.attempts = "queued", None, max(0, job.attempts - 1)


def retry(db: Session, job: OcrJob) -> None:
    """Read again from scratch (a failed job, or a text that came out wrong)."""
    job.status, job.attempts, job.lease_until, job.error = "queued", 0, None, None


def waiting(db: Session) -> int:
    """Jobs not done yet – shown in 📥 and to the worker."""
    return db.scalar(select(func.count(OcrJob.id)).where(OcrJob.status.in_(("queued", "working")))) or 0
