#!/usr/bin/env python3
"""Reads documents queued in Domownik's 📥 Do rozpoznania with a local model – runs on the PC, not the server.

Loop: ask the server for the next job (POST /api/ocr/next), fetch the file, turn it into text, send the text
back. Between jobs it only polls; the model server (llama.cpp with PaddleOCR-VL, a systemd user unit) is
started for the first job and stopped again after IDLE_STOP seconds without work, so the graphics card is
free the rest of the time.

How a file becomes text:
- a PDF with its own text layer (a policy or an invoice from an e-mail) – that text, exact, no model;
- otherwise every page / the photo: turned upright (EXIF), scaled to at most MAX_SIDE, then cut into strips of
  at most STRIP_PIXELS so the model reads them at full resolution (a whole page gets shrunk and small print
  breaks). Cuts go through the emptiest pixel row near the ideal place – between lines of text, not across
  them; where no empty row exists the strips overlap and the repeated lines are dropped when joining.
- a strip on which the model starts repeating itself is read again as two halves.

Never while a game runs: GAME_CHECK (a command exiting 1 when a game runs) is asked before every job, and the
model server is stopped right away. The tray menu of the local models stops this whole service as well.

Configuration (environment, e.g. ~/.config/domownik-ocr.env – never in git):
  DOMOWNIK_URL          https://domownik.<tailnet>.ts.net
  DOMOWNIK_OCR_TOKEN    the same value as DOMOWNIK_OCR_TOKEN on the server
  OCR_URL               http://127.0.0.1:8180 (llama-server with PaddleOCR-VL)
  OCR_UNIT              ocr-server.service (systemd --user unit of that server)
  OCR_MODEL_NAME        PaddleOCR-VL-1.6 (stored with the text)
  GAME_CHECK            e.g. "~/voice-agents/.venv/bin/python -m voice_agents.gamewatch" (empty = no check)
Status for the tray: $XDG_RUNTIME_DIR/domownik-ocr.json
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger("ocr-worker")

POLL = 60  # seconds between asking for work when the queue is empty
IDLE_STOP = 300  # stop the model server after this long without work
MAX_SIDE = 1800  # px – longer side of a photo / rendered page (receipts read best at 1600–1800)
STRIP_PIXELS = 900_000  # PaddleOCR-VL reads up to ~1 Mpx without shrinking
OVERLAP = 40  # px – only where no empty row was found
PDF_DPI = 200
PDF_MAX_PAGES = 10
MIN_PDF_TEXT = 30  # characters per page – fewer means a scanned PDF
MAX_TOKENS = 2048
EDGE = 40  # edge strength that counts as a letter's edge
BLANK_ROW = 0.002  # share of edge pixels in a row still counted as empty
SAME_LINE = 0.75  # similarity of two readings of one line


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


SERVER = env("DOMOWNIK_URL").rstrip("/")
TOKEN = env("DOMOWNIK_OCR_TOKEN")
OCR_URL = env("OCR_URL", "http://127.0.0.1:8180").rstrip("/")
OCR_UNIT = env("OCR_UNIT", "ocr-server.service")
MODEL_NAME = env("OCR_MODEL_NAME", "PaddleOCR-VL-1.6")
GAME_CHECK = shlex.split(os.path.expanduser(env("GAME_CHECK")))
STATUS = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "domownik-ocr.json"


# ---------------------------------------------------------------- status, game, model server


def status(state: str, waiting: int | None = None) -> None:
    """What the tray shows: idle / reading / game / offline / error, and how many documents wait."""
    try:
        STATUS.write_text(json.dumps({"state": state, "waiting": waiting, "at": int(time.time())}))
    except OSError:
        pass


def game_running() -> bool:
    if not GAME_CHECK:
        return False
    return subprocess.run(GAME_CHECK, capture_output=True).returncode != 0


def systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", "--user", *args, OCR_UNIT], capture_output=True).returncode


def model_up() -> bool:
    try:
        with urllib.request.urlopen(f"{OCR_URL}/health", timeout=3) as r:
            return r.status == 200
    except urllib.error.URLError, OSError:
        return False


def start_model() -> None:
    if model_up():
        return
    log.info("starting %s", OCR_UNIT)
    systemctl("start")
    for _ in range(120):
        if model_up():
            return
        state = subprocess.run(["systemctl", "--user", "is-active", OCR_UNIT], capture_output=True, text=True)
        if state.stdout.strip() in ("inactive", "failed"):  # refused (a game – ExecCondition) or crashed
            raise Interrupted("serwer modelu się nie uruchomił")
        time.sleep(1)
    raise RuntimeError("serwer modelu nie odpowiada")


def stop_model() -> None:
    if systemctl("is-active", "--quiet") == 0:
        log.info("stopping %s", OCR_UNIT)
        systemctl("stop")


class Interrupted(Exception):
    """Not the document's fault (a game started, the service is being stopped): the job goes back to the queue
    without using up one of its attempts."""


# ---------------------------------------------------------------- the server's API


def api(method: str, path: str, body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SERVER}/api/ocr{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req, timeout=timeout)


def next_job() -> tuple[dict | None, int]:
    with api("POST", "/next") as r:
        waiting = int(r.headers.get("X-Waiting") or 0)
        return (json.load(r) if r.status == 200 else None), waiting


# ---------------------------------------------------------------- file → images or text


def pdf_text(path: Path) -> tuple[str, int]:
    info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True).stdout
    pages = next((int(line.split()[-1]) for line in info.splitlines() if line.startswith("Pages:")), 1)
    text = subprocess.run(
        ["pdftotext", "-layout", "-l", str(PDF_MAX_PAGES), str(path), "-"], capture_output=True, text=True
    ).stdout
    return text, pages


def pdf_images(path: Path, workdir: Path) -> list[Image.Image]:
    subprocess.run(
        ["pdftoppm", "-r", str(PDF_DPI), "-l", str(PDF_MAX_PAGES), "-png", str(path), str(workdir / "p")],
        check=True,
        capture_output=True,
    )
    return [Image.open(p) for p in sorted(workdir.glob("p*.png"))]


def prepare(im: Image.Image) -> Image.Image:
    im = ImageOps.exif_transpose(im).convert("RGB")
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    return im


def row_ink(im: Image.Image) -> list[float]:
    """How much text every row holds (0 = a gap between lines). Edges, not darkness: on a photo the paper is
    grey, shadowed and creased, but only letters have sharp edges."""
    edges = im.convert("L").filter(ImageFilter.FIND_EDGES).point(lambda v: 255 if v > EDGE else 0)
    column = edges.resize((1, im.height), Image.Resampling.BOX)
    return [v / 255 for v in column.tobytes()]  # mode L: one byte per row


def cut_points(im: Image.Image) -> list[tuple[int, int]]:
    """(top, bottom) of every strip: each ≤ STRIP_PIXELS, cut through the emptiest row near the ideal place."""
    w, h = im.size
    n = max(1, math.ceil(w * h / STRIP_PIXELS))
    if n == 1:
        return [(0, h)]
    ink = row_ink(im)
    step = h / n
    cuts: list[tuple[int, bool]] = []  # (row, empty?)
    for k in range(1, n):
        ideal = int(k * step)
        lo, hi = max(1, int(ideal - step * 0.2)), min(h - 1, int(ideal + step * 0.1))  # never taller than allowed
        best = min(range(lo, hi + 1), key=lambda y: (ink[y], abs(y - ideal)))
        cuts.append((best, ink[best] <= BLANK_ROW))
    strips = []
    top = 0
    for row, empty in cuts:
        strips.append((top, row if empty else min(h, row + OVERLAP)))
        top = row if empty else max(0, row - OVERLAP)
    strips.append((top, h))
    return strips


# ---------------------------------------------------------------- the model


def ask_model(im: Image.Image) -> tuple[str, bool]:
    """(text, cut short): the strip's text and whether the model ran out of tokens (usually a loop)."""
    buf = io.BytesIO()
    im.save(buf, "PNG")
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()},
                    },
                    {"type": "text", "text": "OCR:"},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "repeat_penalty": 1.1,
    }
    req = urllib.request.Request(
        f"{OCR_URL}/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.load(r)
    choice = out["choices"][0]
    return choice["message"]["content"] or "", choice.get("finish_reason") == "length"


def looks_looped(text: str) -> bool:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 10 and len(set(lines)) <= len(lines) // 4:
        return True
    words = text.split()
    return len(words) >= 60 and len(set(words)) <= len(words) // 8


def read_strip(im: Image.Image, depth: int = 0) -> str:
    text, cut = ask_model(im)
    if (cut or looks_looped(text)) and depth < 2 and im.height > 120:
        log.info("strip %dx%d looped – reading it as two halves", *im.size)
        half = im.height // 2
        return join(
            [
                read_strip(im.crop((0, 0, im.width, half + OVERLAP)), depth + 1),
                read_strip(im.crop((0, half - OVERLAP, im.width, im.height)), depth + 1),
            ]
        )
    return text


def _norm(line: str) -> str:
    return " ".join(line.split()).lower()


def _same(a: str, b: str) -> bool:
    """The same line read twice (in two overlapping strips) – the model may read it a little differently."""
    a, b = _norm(a), _norm(b)
    return bool(a) and (a == b or (min(len(a), len(b)) >= 6 and SequenceMatcher(None, a, b).ratio() >= SAME_LINE))


def join(parts: list[str]) -> str:
    """Join strips; lines repeated across an overlap (end of one ≈ start of the next) are kept once – the copy
    from the strip where the line was whole (the longer one)."""
    out: list[str] = []
    for part in parts:
        lines = [ln for ln in part.strip().splitlines()]
        tail = [i for i in range(max(0, len(out) - 6), len(out)) if out[i].strip()]
        drop = 0
        for i, line in enumerate(lines[:6]):
            match = next((j for j in tail if _same(out[j], line)), None)
            if match is not None:
                if len(_norm(line)) > len(_norm(out[match])):
                    out[match] = line
                drop = i + 1
        if out and lines[drop:]:
            out.append("")
        out.extend(lines[drop:])
    return "\n".join(out).strip()


def read_image(im: Image.Image) -> str:
    im = prepare(im)
    return join([read_strip(im.crop((0, top, im.width, bottom))) for top, bottom in cut_points(im)])


def read_file(path: Path, content_type: str, workdir: Path) -> dict:
    if content_type == "application/pdf":
        text, pages = pdf_text(path)
        if len(text.strip()) >= MIN_PDF_TEXT * min(pages, PDF_MAX_PAGES):
            return {"text": text, "source": "pdf-text", "model": None, "pages": pages}
        start_model()
        images = pdf_images(path, workdir)
        texts = [read_image(im) for im in images]
        return {"text": "\n\n".join(texts), "source": "ocr", "model": MODEL_NAME, "pages": len(images)}
    start_model()
    return {"text": read_image(Image.open(path)), "source": "ocr", "model": MODEL_NAME, "pages": 1}


# ---------------------------------------------------------------- main loop


def handle(job: dict) -> None:
    job_id = job["id"]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="domownik-ocr-") as tmp:
        workdir = Path(tmp)
        path = workdir / "doc"
        with api("GET", f"/{job_id}/plik", timeout=120) as r:
            path.write_bytes(r.read())
        try:
            result = read_file(path, job["content_type"], workdir)
        except (Interrupted, SystemExit, KeyboardInterrupt) as exc:
            log.info("job %s interrupted (%s) – giving it back", job_id, exc or exc.__class__.__name__)
            with api("POST", f"/{job_id}/wynik", {"release": True}, timeout=10):
                pass
            raise
        except Exception as exc:  # any failure goes back to the server, which retries it
            if game_running():
                raise Interrupted("gra") from exc
            log.exception("job %s failed", job_id)
            result = {"error": str(exc)[:300] or exc.__class__.__name__}
    with api("POST", f"/{job_id}/wynik", result) as r:
        answer = json.load(r)
    log.info(
        "job %s (%s): %s, %s, %.1f s",
        job_id,
        job["content_type"],
        answer.get("status"),
        result.get("source") or result.get("error"),
        time.monotonic() - started,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # stopped from the tray menu or for a game: SystemExit lets a job in progress be given back
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not SERVER or not TOKEN:
        raise SystemExit("DOMOWNIK_URL i DOMOWNIK_OCR_TOKEN muszą być ustawione")
    last_work = 0.0
    while True:
        if game_running():
            stop_model()
            status("game")
            time.sleep(30)
            continue
        try:
            job, waiting = next_job()
        except (urllib.error.URLError, OSError) as exc:
            log.warning("server unreachable: %s", exc)
            status("offline")
            time.sleep(POLL)
            continue
        if job is None:
            if time.monotonic() - last_work > IDLE_STOP:
                stop_model()
            status("idle", waiting)
            time.sleep(POLL)
            continue
        status("reading", waiting)
        try:
            handle(job)
        except Interrupted:
            continue  # the game check at the top of the loop takes over
        except (urllib.error.URLError, OSError) as exc:
            log.warning("job %s: lost the server (%s) – it will come back to the queue", job.get("id"), exc)
            time.sleep(POLL)
        last_work = time.monotonic()


if __name__ == "__main__":
    main()
