from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from markupsafe import escape
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from domownik.config import get_settings, now
from domownik.domain.money import InvalidAmount
from domownik.routers import (
    account,
    assignments,
    auth,
    bank,
    board,
    budget,
    calendar,
    calendar_ext,
    chores,
    comments,
    contacts,
    dashboard,
    documents,
    emergency,
    forma,
    goals,
    inbox,
    items,
    kitchen,
    meters,
    points,
    quick,
    schedule,
    shopping,
    tasks,
    timers,
)
from domownik.security import LoginRequired
from domownik.slowlog import SlowRequestLog
from domownik.web import HERE, is_htmx, render

log = logging.getLogger("domownik")


def _tick() -> None:
    from domownik import notify
    from domownik.db import session_factory

    try:
        with session_factory()() as db:
            notify.tick(db, now())
    except Exception:  # a failing job must never take the scheduler down
        log.exception("scheduler tick failed")


def _timer_tick() -> None:
    from domownik import supplements, timers
    from domownik.db import session_factory

    try:
        with session_factory()() as db:
            timers.run(db)
    except Exception:
        log.exception("timer tick failed")
    try:
        with session_factory()() as db:
            supplements.run(db)
    except Exception:  # separately: a broken supplement push must not stop the timers
        log.exception("supplement tick failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    from domownik import ics

    ics.install_log_filter()  # after uvicorn has set up its loggers: no calendar tokens in the access log
    scheduler = None
    if settings.scheduler:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler(timezone=settings.tz)
        # every 5 minutes; the first run right after start catches up on a missed morning
        scheduler.add_job(_tick, "interval", minutes=5, next_run_time=now(), max_instances=1, coalesce=True)
        # timers ("pranie – 2 h") need the minute, not the five: a cheap query on an indexed column
        scheduler.add_job(_timer_tick, "interval", minutes=1, next_run_time=now(), max_instances=1, coalesce=True)
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


# Sent with every response (a route may set its own, e.g. the stricter CSP of an opened photo).
# No script-src: base.html has a tiny inline theme script and htmx's hx-on needs eval – the rest still
# blocks framing (clickjacking), <base> / form hijacking and plugins.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
    "Permissions-Policy": "camera=(self), microphone=(self), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
HSTS = "max-age=31536000"

# Garbage in a form (a hand-edited request, an old cached page): "int('abc')", a date like 2026-02-30,
# a number too big for SQLite, an id of something deleted meanwhile – a 400 with a message, never a 500.
BAD_INPUT = "Nieprawidłowe dane w formularzu – odśwież stronę i spróbuj ponownie."


UNEXPECTED = "Coś poszło nie tak po stronie Domownika. Spróbuj ponownie za chwilę."
UNEXPECTED_PAGE = (
    '<!doctype html><html lang="pl"><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Błąd · Domownik</title>'
    '<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem">'
    f'<h2>Błąd 500</h2><p>{UNEXPECTED}</p><p><a href="/">← Wróć na pulpit</a></p></body></html>'
)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Domownik", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if settings.https_only:
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.resolved_secret_key(),
        session_cookie="domownik_session",
        max_age=settings.session_days * 86400,
        same_site="lax",
        https_only=settings.https_only,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(items.router)
    app.include_router(items.issues_router)
    app.include_router(items.notes_router)
    app.include_router(tasks.router)
    app.include_router(shopping.router)
    app.include_router(kitchen.router)
    app.include_router(board.router)
    app.include_router(account.router)
    app.include_router(assignments.router)
    app.include_router(calendar_ext.router)
    app.include_router(calendar.router)
    app.include_router(bank.router)
    app.include_router(budget.router)
    app.include_router(goals.router)
    app.include_router(documents.router)
    app.include_router(schedule.router)
    app.include_router(chores.router)
    app.include_router(points.router)
    app.include_router(comments.router)
    app.include_router(quick.router)
    app.include_router(contacts.router)
    app.include_router(emergency.router)
    app.include_router(inbox.router)
    app.include_router(meters.router)
    app.include_router(timers.router)
    app.include_router(forma.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        if is_htmx(request):
            return HTMLResponse("", status_code=401, headers={"HX-Redirect": "/login"})
        if request.url.path.startswith("/api/"):
            return HTMLResponse("Unauthorized", status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(InvalidAmount)
    async def _invalid_amount(request: Request, exc: InvalidAmount):
        # safety net for every form with a money field: a readable message instead of a 500;
        # htmx leaves the form as it was (nothing swapped) and app.js shows the text as a toast
        if is_htmx(request) or request.url.path.startswith("/api/"):
            return HTMLResponse(str(exc), status_code=400, headers={"HX-Reswap": "none"})
        return render(request, "error.html", {"code": 400, "detail": str(exc)}, status_code=400)

    @app.exception_handler(ValueError)  # also json.JSONDecodeError
    @app.exception_handler(OverflowError)
    @app.exception_handler(IntegrityError)
    async def _bad_input(request: Request, exc: Exception):
        log.warning("bad input on %s %s: %s", request.method, request.url.path, exc, exc_info=exc)
        if is_htmx(request) or request.url.path.startswith(("/api/", "/push/")):
            return HTMLResponse(BAD_INPUT, status_code=400, headers={"HX-Reswap": "none"})
        return render(request, "error.html", {"code": 400, "detail": BAD_INPUT}, status_code=400)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # a bug: the traceback still goes to the log (Starlette re-raises after this answer); the page is
        # plain HTML on purpose – rendering the usual layout needs the database, which may be what broke
        log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        if is_htmx(request) or request.url.path.startswith(("/api/", "/push/")):
            return HTMLResponse(UNEXPECTED, status_code=500, headers={"HX-Reswap": "none"})
        return HTMLResponse(UNEXPECTED_PAGE, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def _bad_url(request: Request, exc: RequestValidationError):
        # a hand-typed / cut-off link: "/items/abc" is a page that does not exist, "/kalendarz?d=abc" a bad
        # address – a normal error page instead of FastAPI's JSON
        in_path = any(e.get("loc", ("",))[0] == "path" for e in exc.errors())
        code = 404 if in_path else 400
        detail = "Nie ma takiej strony." if in_path else "Nieprawidłowy adres – brakuje czegoś albo coś jest w nim źle."
        if is_htmx(request) or request.url.path.startswith(("/api/", "/push/")):
            return HTMLResponse(detail, status_code=code, headers={"HX-Reswap": "none"})
        return render(request, "error.html", {"code": code, "detail": detail}, status_code=code)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if is_htmx(request) or request.url.path.startswith("/api/"):
            # details may quote what the user typed ("Invalid isoformat string: '<…>'")
            return HTMLResponse(f'<p class="error">{escape(exc.detail)}</p>', status_code=exc.status_code)
        return render(
            request, "error.html", {"code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code
        )

    # outermost: times the whole request, session and security headers included
    app.add_middleware(SlowRequestLog, threshold_ms=settings.slow_ms)
    return app


app = create_app()
