from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik import services
from domownik.config import today
from domownik.db import get_db
from domownik.domain.tree import Tree
from domownik.models import MEMBER_KINDS, Item, User
from domownik.routers.shopping import open_items
from domownik.security import current_user, verify_csrf
from domownik.web import render

router = APIRouter()

# "Później" can hold dozens of rows – the rest hides behind "Pokaż wszystkie (N)"
LATER_SHOWN = 5


@router.get("/")
def dashboard(
    request: Request,
    who: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    t = today()
    services.settle_autopay(db, t)
    tree = Tree.load(db)
    # household members get their own row; top-level members are not shown again as zones
    roots = [i for i in tree.roots() if i.kind != "member"]
    members = sorted(
        (i for i in tree.items.values() if i.is_member),
        key=lambda i: (MEMBER_KINDS.index(i.kind), i.sort_order, i.name),
    )
    rows = services.task_rows(db, tree, t)
    if who == "me":
        rows = [r for r in rows if r.task.assignee_id in (None, user.id)]
    buckets: dict[str, list] = {"overdue": [], "week": [], "month": [], "later": []}
    for r in rows:
        buckets[r.due.bucket].append(r)
    # many payments on one day (rent, subscriptions…) collapse into one row each
    grouped = {k: services.group_payments(v) for k, v in buckets.items()}
    stats = services.zone_stats(db, tree, roots + members, t)
    todos = services.open_todos(db, tree)
    if who == "me":
        todos = [td for td in todos if td.assignee_id in (None, user.id)]
    has_items = db.scalar(select(func.count(Item.id))) > 0
    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "roots": roots,
            "members": members,
            "todos": todos,
            "shopping": [s for s in open_items(db) if s.checked_at is None],
            "notes": services.active_notes(db, t),
            "label": tree.label,
            "stats": stats,
            "buckets": buckets,
            "grouped": grouped,
            "has_items": has_items,
            "who": who,
            "any_assigned": services.any_assigned(db),
            "timeline_item": None,
            "LATER_SHOWN": LATER_SHOWN,
        },
    )


@router.post("/setup/zones", dependencies=[Depends(verify_csrf)])
def setup_zones(db: Session = Depends(get_db), user: User = Depends(current_user)):
    if db.scalar(select(func.count(Item.id))) == 0:
        services.seed_default_zones(db, today())
    resp = RedirectResponse("/", status_code=303)
    resp.headers["HX-Redirect"] = "/"
    return resp


@router.get("/api/timeline")
def api_timeline(
    start: date = Query(...),
    end: date = Query(...),
    item: int | None = None,
    bin: str | None = Query(None, pattern="^(day|week|month)$"),
    lanes: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Timeline points; with `bin` ordinary points of one day/week/month come merged into labelled bundles."""
    if end < start or end - start > timedelta(days=366 * 6):
        raise HTTPException(400, "Zakres maks. 6 lat")
    services.settle_autopay(db, today())
    tree = Tree.load(db)
    ids = tree.subtree_ids(item) if item else None
    data = services.timeline(db, tree, today(), start, end, ids)
    if bin:
        data["points"] = services.bundle_points(data["points"], bin, by_group=lanes)
    data["today"] = today().isoformat()
    return JSONResponse(data)


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"ok": True}
