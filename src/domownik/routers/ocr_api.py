"""/api/ocr – the worker on the PC takes queued documents and returns their text (see domownik.ocr).

Only with `Authorization: Bearer <DOMOWNIK_OCR_TOKEN>`; without a configured token the API does not exist.
The worker is not a household member: it reads every queued file, private ones too, and writes only the text,
which is shown with the attachment to whoever may see the attachment.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from domownik import documents, ocr
from domownik.config import get_settings
from domownik.db import get_db
from domownik.models import OcrJob

router = APIRouter(prefix="/api/ocr")


def worker(request: Request) -> None:
    token = get_settings().ocr_token
    if not token:
        raise HTTPException(404)
    given = request.headers.get("authorization", "")
    if not secrets.compare_digest(given.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401)


def _job(db: Session, job_id: int) -> OcrJob:
    job = db.get(OcrJob, job_id)
    if job is None or job.status != "working":
        raise HTTPException(409, "To zadanie nie jest już w odczycie")
    return job


@router.post("/next", dependencies=[Depends(worker)])
def take_next(db: Session = Depends(get_db)):
    job = ocr.claim(db)
    db.commit()
    if job is None:
        return Response(status_code=204, headers={"X-Waiting": "0"})
    att = job.attachment
    return JSONResponse(
        {"id": job.id, "content_type": att.content_type, "name": att.original_name, "attempt": job.attempts},
        headers={"X-Waiting": str(ocr.waiting(db))},
    )


@router.get("/{job_id}/plik", dependencies=[Depends(worker)])
def file(job_id: int, db: Session = Depends(get_db)):
    att = _job(db, job_id).attachment
    try:
        path = documents.file_path(att)
    except FileNotFoundError:
        raise HTTPException(404) from None
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=att.content_type)


class Result(BaseModel):
    text: str | None = Field(default=None, max_length=ocr.MAX_TEXT * 2)
    source: str | None = None
    model: str | None = None
    pages: int | None = None
    error: str | None = None
    # the worker was interrupted (a game, the PC shutting down): back to the queue, the attempt does not count
    release: bool = False


@router.post("/{job_id}/wynik", dependencies=[Depends(worker)])
def result(job_id: int, body: Result, db: Session = Depends(get_db)):
    job = _job(db, job_id)
    if body.release:
        ocr.release(db, job)
        db.commit()
        return {"status": job.status}
    ocr.finish(db, job, text=body.text, source=body.source, model=body.model, pages=body.pages, error=body.error)
    db.commit()
    return {"status": job.status}
