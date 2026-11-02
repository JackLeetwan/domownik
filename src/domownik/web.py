"""Jinja environment and small view helpers shared by routers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from domownik.config import today
from domownik.domain import recurrence
from domownik.domain.money import format_pln, input_pln
from domownik.domain.status import BUCKETS, STATUS_LABELS, humanize_days
from domownik.models import CATEGORIES, DIRECTIONS, ITEM_KINDS
from domownik.security import csrf_token

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

MONTHS = ["", "sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru"]
WEEKDAYS_SHORT = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]


def fmt_date(d: date | None, *, weekday: bool = False) -> str:
    if d is None:
        return "—"
    text = f"{d.day} {MONTHS[d.month]}"
    if d.year != today().year:
        text += f" {d.year}"
    if weekday:
        text = f"{WEEKDAYS_SHORT[d.weekday()]}, {text}"
    return text


env = templates.env
env.filters["pln"] = format_pln
env.filters["pln_input"] = input_pln
env.filters["d"] = fmt_date
env.filters["days"] = humanize_days
env.filters["rule"] = recurrence.describe
env.globals.update(
    CATEGORIES=CATEGORIES,
    DIRECTIONS=DIRECTIONS,
    ITEM_KINDS=ITEM_KINDS,
    STATUS_LABELS=STATUS_LABELS,
    BUCKETS=BUCKETS,
    UNIT_LABELS=recurrence.UNIT_LABELS,
    WEEKDAY_LABELS=recurrence.WEEKDAY_LABELS,
    MONTH_GENITIVE=recurrence.MONTH_GENITIVE,
    today=today,
)


def _nav(name: str, ctx: dict):
    """Sidebar data for full pages of a logged-in user (not for HTMX fragments)."""
    if not ctx.get("user") or name.startswith("partials/"):
        return None
    from domownik import services  # local: services imports models, avoid import cycles at module load
    from domownik.db import session_factory

    with session_factory()() as db:
        return services.nav_context(db, today())


def render(request: Request, name: str, ctx: dict | None = None, *, status_code: int = 200, headers=None):
    context = {"csrf": csrf_token(request)}
    context.update(ctx or {})
    if "nav" not in context:
        context["nav"] = _nav(name, context)
    return templates.TemplateResponse(request, name, context, status_code=status_code, headers=headers)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"
