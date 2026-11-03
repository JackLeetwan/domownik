from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from domownik.config import get_settings, now
from domownik.domain.money import InvalidAmount
from domownik.routers import (
    account,
    assignments,
    auth,
    board,
    budget,
    calendar,
    chores,
    contacts,
    dashboard,
    documents,
    goals,
    items,
    kitchen,
    meters,
    quick,
    schedule,
    shopping,
    tasks,
)
from domownik.security import LoginRequired
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
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Domownik", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
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
    app.include_router(calendar.router)
    app.include_router(budget.router)
    app.include_router(goals.router)
    app.include_router(documents.router)
    app.include_router(schedule.router)
    app.include_router(chores.router)
    app.include_router(quick.router)
    app.include_router(contacts.router)
    app.include_router(meters.router)

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

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if is_htmx(request) or request.url.path.startswith("/api/"):
            return HTMLResponse(f'<p class="error">{exc.detail}</p>', status_code=exc.status_code)
        return render(
            request, "error.html", {"code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code
        )

    return app


app = create_app()
