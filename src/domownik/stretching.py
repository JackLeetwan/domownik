"""🤸 Stretching in "Forma": the library of stretches and saved routines ("Przed treningiem", "Po treningu").

The library is shared by the household and seeded from catalog/seed/stretches.yaml (new keys are added on start,
names and defaults follow the catalog). Routines are shared too, like the strength plans; the two default ones
are made on the first visit and then changed freely.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domownik import training as tr
from domownik.models import Stretch, StretchRoutine, StretchRoutineItem

CATALOG = Path(__file__).parent / "catalog" / "seed" / "stretches.yaml"
TRANSITION = 5  # seconds to get into the next position (step 3: the timer)


@functools.cache
def catalog() -> dict[str, Any]:
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))


@functools.cache
def _by_key() -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in catalog()["stretches"]}


def type_label(key: str) -> str:
    return catalog()["types"].get(key, key)


def area_label(key: str) -> str:
    return catalog()["areas"].get(key, key)


# ---------------------------------------------------------------- pictures and instructions


def _frames(s: Stretch) -> tuple[str, str] | None:
    row = _by_key().get(s.key)
    if not row or not row.get("img"):
        return None
    slug = tr._slug(row["img"])
    return f"/static/exercises/{slug}.webp", f"/static/exercises/{slug}-1.webp"


def image(s: Stretch) -> str | None:
    """The icon: the frame that shows the stretch (the start of some is just a person standing)."""
    frames = _frames(s)
    if frames is None:
        return None
    return frames[1] if _by_key()[s.key].get("pic") == 1 else frames[0]


def image_start(s: Stretch) -> str | None:
    frames = _frames(s)
    return frames[0] if frames else None


def image_end(s: Stretch) -> str | None:
    frames = _frames(s)
    return frames[1] if frames else None


def icon(s: Stretch) -> str:
    return "🤸" if s.type == "dynamic" else "🧘"


def guide(s: Stretch) -> tr.Guide | None:
    return tr.stretch_guides().get(s.key)


# ---------------------------------------------------------------- library and routines


def ensure_library(db: Session) -> None:
    """Seeded stretches missing in the database are added; the existing ones follow the catalog."""
    have = {s.key: s for s in db.scalars(select(Stretch))}
    for row in catalog()["stretches"]:
        s = have.get(row["key"]) or Stretch(key=row["key"])
        s.name, s.type, s.area = row["name"], row["type"], row["area"]
        s.sided, s.seconds = bool(row.get("sided")), int(row["seconds"])
        if s.id is None:
            db.add(s)
    db.flush()


def seed_routines(db: Session) -> list[StretchRoutine]:
    """The default routines – once, when there are none at all (also none archived)."""
    if db.scalar(select(StretchRoutine.id).limit(1)) is not None:
        return []
    ensure_library(db)
    by_key = {s.key: s for s in db.scalars(select(Stretch))}
    made = []
    for pos, row in enumerate(catalog()["routines"]):
        r = StretchRoutine(name=row["name"], kind=row["kind"], position=pos)
        r.items = [StretchRoutineItem(stretch=by_key[k], position=i) for i, k in enumerate(row["items"])]
        db.add(r)
        made.append(r)
    db.flush()
    return made


def prepare(db: Session) -> None:
    ensure_library(db)
    seed_routines(db)


def routines(db: Session) -> list[StretchRoutine]:
    return list(
        db.scalars(
            select(StretchRoutine)
            .where(StretchRoutine.archived.is_(False))
            .options(selectinload(StretchRoutine.items).selectinload(StretchRoutineItem.stretch))
            .order_by(StretchRoutine.position, StretchRoutine.id)
        )
    )


def for_kind(db: Session, kind: str) -> StretchRoutine | None:
    """The routine the workout offers before / after (the first one of that kind)."""
    return next((r for r in routines(db) if r.kind == kind), None)


def library(db: Session) -> list[Stretch]:
    order = {row["key"]: i for i, row in enumerate(catalog()["stretches"])}
    return sorted(db.scalars(select(Stretch)), key=lambda s: order.get(s.key, len(order)))


def groups(db: Session, type_: str | None) -> list[tuple[str, list[Stretch]]]:
    """The picker: stretches by area (in the catalog's order of areas), optionally only dynamic / static."""
    rows = [s for s in library(db) if type_ is None or s.type == type_]
    return [(label, [s for s in rows if s.area == k]) for k, label in catalog()["areas"].items()]


def steps_count(item: StretchRoutineItem) -> int:
    return 2 if item.stretch.sided else 1


def total_seconds(r: StretchRoutine) -> int:
    """How long the routine takes: every side of every stretch + a transition before each but the first."""
    work = sum(it.duration * steps_count(it) for it in r.items)
    moves = sum(steps_count(it) for it in r.items) - 1
    return work + max(0, moves) * TRANSITION


def fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    if not m:
        return f"{s} s"
    return f"{m} min" if not s else f"{m} min {s} s"
