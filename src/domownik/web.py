"""Jinja environment and small view helpers shared by routers."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from domownik import menu
from domownik.config import today
from domownik.domain import holidays, recurrence
from domownik.domain.money import format_pln, input_pln
from domownik.domain.status import BUCKETS, STATUS_LABELS, humanize_days
from domownik.models import CATEGORIES, DIRECTIONS, ITEM_KINDS, MEALS, NOTE_COLORS
from domownik.security import csrf_token

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
# the asset versions bumped in base.html ("set css_v, js_v = 101, 61") – also sent with every htmx answer
# (X-Asset-V, main.py): a page opened before a deploy reloads itself even when only a part of it is swapped
ASSET_V = "-".join(
    re.search(r"set css_v, js_v = (\d+), (\d+)", (HERE / "templates" / "base.html").read_text("utf-8")).groups()
)

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


AVATAR_COLORS = 6  # .av-0 … .av-5 in app.css – readable with white text in both themes


def initials(name: str | None) -> str:
    """'Ala' → 'A', 'Ala Nowak' → 'AN'."""
    parts = (name or "?").split()
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def avatar_class(user_id: int) -> str:
    """Fixed colour per account – the same person looks the same everywhere."""
    return f"av-{(user_id - 1) % AVATAR_COLORS}"


def avatar(user, *, size: str = "") -> Markup:
    """Small coloured circle with the initials of the person a task is assigned to."""
    if user is None:
        return Markup("")
    cls = f"avatar {avatar_class(user.id)}" + (f" avatar-{size}" if size else "")
    name = escape(user.name)
    return Markup(
        f'<span class="{cls}" title="Zajmuje się: {name}" aria-label="Zajmuje się: {name}">'
        f"{escape(initials(user.name))}</span>"
    )


def other_avatar(assignee, me) -> Markup:
    """Initials only when someone else takes care of it – your own tasks (and shared ones) stay unmarked,
    the same split as the "Moje" filter, so lists are not a column of identical circles."""
    if assignee is None or (me is not None and getattr(me, "id", None) == assignee.id):
        return Markup("")
    return avatar(assignee)


def week_title(monday) -> str:
    """ "2–8 listopada", "30 listopada – 6 grudnia" (+ the year when it is not this one) – one form for every week view."""
    from datetime import timedelta

    sunday = monday + timedelta(days=6)
    g = recurrence.MONTH_GENITIVE
    if monday.month == sunday.month:
        text = f"{monday.day}–{sunday.day} {g[sunday.month]}"
    else:
        text = f"{monday.day} {g[monday.month]} – {sunday.day} {g[sunday.month]}"
    return text + (f" {sunday.year}" if sunday.year != today().year else "")


def items_text(items: list[dict] | None) -> str:
    """A checklist note as editable text: one point per line – ✓ ticked, ☐ open (parse_items reads both)."""
    return "\n".join(("✓ " if i.get("d") else "☐ ") + i.get("t", "") for i in items or [])


env = templates.env


def _exercise_image(ex):
    """The icon of an exercise – a strength one (Exercise) or a stretch (Stretch)."""
    from domownik import stretching, training
    from domownik.models import Stretch

    return stretching.image(ex) if isinstance(ex, Stretch) else training.image(ex)


def _exercise_image_end(ex):
    """The end frame of a strength exercise (None for a stretch or without one)."""
    from domownik import training
    from domownik.models import Exercise

    return training.image_end(ex) if isinstance(ex, Exercise) else None


def _exercise_icon(ex):
    from domownik import stretching, training
    from domownik.models import Stretch

    return stretching.icon(ex) if isinstance(ex, Stretch) else training.equipment_icon(ex)


env.filters["pln"] = format_pln
env.filters["pln_input"] = input_pln
env.filters["d"] = fmt_date
env.filters["days"] = humanize_days
env.filters["rule"] = recurrence.describe
env.filters["week"] = week_title
env.globals.update(
    exercise_image=_exercise_image,
    exercise_image_end=_exercise_image_end,
    exercise_icon=_exercise_icon,
    CATEGORIES=CATEGORIES,
    DIRECTIONS=DIRECTIONS,
    ITEM_KINDS=ITEM_KINDS,
    STATUS_LABELS=STATUS_LABELS,
    BUCKETS=BUCKETS,
    UNIT_LABELS=recurrence.UNIT_LABELS,
    WEEKDAY_LABELS=recurrence.WEEKDAY_LABELS,
    MONTH_GENITIVE=recurrence.MONTH_GENITIVE,
    today=today,
    avatar=avatar,
    other_avatar=other_avatar,
    avatar_class=avatar_class,
    initials=initials,
    holiday=holidays.name,
    menu_bar=menu.bar,
    menu_sidebar=menu.sidebar,
    menu_groups=menu.sidebar_groups,
    items_text=items_text,
    NOTE_COLORS=NOTE_COLORS,
    MEALS=MEALS,
)


def _nav(request: Request, name: str, ctx: dict):
    """Sidebar data for full pages of a logged-in user (not for HTMX fragments)."""
    if not ctx.get("user") or name.startswith("partials/"):
        return None
    from domownik import services  # local: services imports models, avoid import cycles at module load
    from domownik.db import VIEWER, session_factory, set_viewer

    uid = ctx["user"].id
    # the page's own session when it already looks as this user: what the page has just read
    # (zone tree, notes, todos, chores) is not read again
    db = getattr(request.state, "db", None)
    if db is not None and db.info.get(VIEWER) == uid:
        return services.nav_context(db, today(), uid)
    with session_factory()() as db:
        set_viewer(db, uid)
        return services.nav_context(db, today(), uid)


def render(request: Request, name: str, ctx: dict | None = None, *, status_code: int = 200, headers=None):
    context = {"csrf": csrf_token(request)}
    context.update(ctx or {})
    if "nav" not in context:
        context["nav"] = _nav(request, name, context)
    return templates.TemplateResponse(request, name, context, status_code=status_code, headers=headers)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


# ---------------------------------------------------------------- quick add ("＋" on every page)
# Forms in the quick-add window post to the ordinary routes with this header; the route then answers
# with an event instead of a redirect – app.js closes the window, shows "Dodano" and refreshes the page.
QUICK_HEADER = "x-quick-add"
QUICK_ERROR = "#qa-error"


def is_quick(request: Request) -> bool:
    return request.headers.get(QUICK_HEADER) == "1"


def quick_saved(message: str = "Dodano") -> HTMLResponse:
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"dm-added": message}), "HX-Reswap": "none"})


def error_target(request: Request, default: str) -> str:
    """Where a 422 message goes: the quick-add window has its own box (ids on the page may repeat)."""
    return QUICK_ERROR if is_quick(request) else default
