"""📚 One list of routines / plans – 🤸 the stretching routines and 🏋️ the strength plans work the same way.

🔎 the search (in the browser, app.js "data-pick-filter": name, body groups, every exercise in it), the body-group
chips, the kind chips (before / after / own – where there are kinds), one "nothing needed" chip, the order, and
the list split under body-group headings. The page builds `Entry`s from its own objects and renders
templates/partials/browse.html with what `view()` returns.
"""

from __future__ import annotations

import functools
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CATALOG = Path(__file__).parent / "catalog" / "seed" / "body_groups.yaml"
WHOLE = "cale"
WHOLE_LABEL = "🧍 Całe ciało"
SORTS = {"": "Kolejność", "ostatnie": "Ostatnio robione", "krotkie": "Najkrótsze", "dlugie": "Najdłuższe", "az": "A–Z"}


@functools.cache
def groups() -> dict[str, dict[str, Any]]:
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))["groups"]


def group_label(g: str) -> str:
    return WHOLE_LABEL if g == WHOLE else f"{groups()[g]['icon']} {groups()[g]['name']}"


@functools.cache
def _group_of(library: str) -> dict[str, str]:
    return {key: g for g, row in groups().items() for key in row[library]}


def group_of(library: str, key: str) -> str | None:
    """The body group of a stretch's area (library "stretch") or an exercise's muscle ("strength")."""
    return _group_of(library).get(key)


@dataclass
class Entry:
    """One row: a routine or a plan as the list shows it."""

    id: int
    name: str
    href: str
    meta: str  # "9 ćw. · ok. 7 min 50 s"
    amounts: dict[str, float]  # body group → how much of it (seconds, sets)
    position: int
    size: float  # for Najkrótsze / Najdłuższe
    start: str | None = None  # ▶ Start posts here (None – no button)
    start_fields: dict[str, Any] = field(default_factory=dict)
    kind: str | None = None
    free: bool = True  # the "nothing needed" chip keeps it
    search: str = ""  # more words to find it by (its exercises…)

    @functools.cached_property
    def focus(self) -> dict[str, float]:
        """What share each body group gets, the biggest first."""
        total = sum(self.amounts.values())
        if not total:
            return {}
        return {g: v / total for g, v in sorted(self.amounts.items(), key=lambda x: -x[1]) if v}

    @property
    def groups(self) -> list[str]:
        """The groups it is for: each with at least a fifth (the main one always)."""
        return [g for i, (g, share) in enumerate(self.focus.items()) if i == 0 or share >= 0.2]

    @property
    def whole_body(self) -> bool:
        """A tenth or more on the legs and a fifth or more above them (a full-body plan: 1 leg exercise of 7)."""
        return 0.1 <= self.focus.get("legs", 0) <= 0.8

    @property
    def main_group(self) -> str | None:
        return WHOLE if self.whole_body else next(iter(self.focus), None)

    @property
    def label(self) -> str:
        """ "🦵 Nogi i biodra, 🔙 Plecy" – or "🧍 Całe ciało"."""
        return WHOLE_LABEL if self.whole_body else ", ".join(group_label(g) for g in self.groups)

    @property
    def text(self) -> str:
        names = " ".join(groups()[g]["name"] for g in self.groups)
        return f"{self.name} {self.label} {names} {self.search}"

    def in_group(self, g: str) -> bool:
        return self.whole_body if g == WHOLE else g in self.groups


def _plain(text: str) -> str:
    """For A–Z: "Łydki" between "Kolana" and "Mostek", not after "Z"."""
    text = text.lower().replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def chips(entries: list[Entry]) -> list[tuple[str, str, int]]:
    """The body-group chips: (key, label, how many) – only the groups with anything."""
    rows = [(g, group_label(g), sum(1 for e in entries if e.in_group(g))) for g in [WHOLE, *groups()]]
    return [x for x in rows if x[2]]


def sections(entries: list[Entry]) -> list[tuple[str, list[Entry]]]:
    """Split under the main body group (Całe ciało first, then the catalog's order), each keeping the order
    given; an empty one goes under "Inne"."""
    by: dict[str | None, list[Entry]] = {}
    for e in entries:
        by.setdefault(e.main_group, []).append(e)
    out = [(group_label(g), by[g]) for g in [WHOLE, *groups()] if g in by]
    return out + ([("Inne", by[None])] if None in by else [])


def pick(
    entries: list[Entry],
    group: str | None = None,
    kind: str | None = None,
    free: bool = False,
    sort: str = "",
    rank: dict[int, int] | None = None,
) -> list[Entry]:
    """Filtered and ordered. With a body group and the list order, the ones most about that group come first."""
    out = [
        e for e in entries if (not group or e.in_group(group)) and (not kind or e.kind == kind) and (not free or e.free)
    ]
    if sort == "ostatnie":
        rank = rank or {}
        return sorted(out, key=lambda e: (rank.get(e.id, len(rank)), e.position, e.id))
    if sort in ("krotkie", "dlugie"):
        return sorted(out, key=lambda e: (e.size * (1 if sort == "krotkie" else -1), e.position, e.id))
    if sort == "az":
        return sorted(out, key=lambda e: _plain(e.name))
    if group and group != WHOLE:
        return sorted(out, key=lambda e: (-e.focus.get(group, 0), e.position, e.id))
    return out


def _url(base: str, now: dict[str, str | None]) -> Callable[..., str]:
    def url(**change: str | None) -> str:
        """The list with one filter changed and the others kept (None drops it)."""
        q = now | change
        query = "&".join(f"{k}={v}" for k, v in q.items() if v)
        return f"{base}?{query}" if query else base

    return url


def view(
    entries: list[Entry],
    base: str,
    *,
    partia: str = "",
    rodzaj: str = "",
    sprzet: str = "",
    sort: str = "",
    kinds: dict[str, str] | None = None,
    free_label: str = "🙌 Bez sprzętu",
    rank: dict[int, int] | None = None,
    what: str = "zestawu",
) -> dict[str, Any]:
    """Everything partials/browse.html needs, from the page's query (?partia=&rodzaj=&sprzet=0&sort=)."""
    chip_rows = chips(entries)
    group = partia if partia in {k for k, _, _ in chip_rows} else None
    kind = rodzaj if kinds and rodzaj in kinds else None
    free = sprzet == "0"
    sort = sort if sort in SORTS else ""
    shown = pick(entries, group, kind, free, sort, rank)
    return {
        "entries": shown,
        # one body group chosen: one list under its name (its entries are about it, whatever their main group)
        "sections": [(group_label(group), shown)] if group and shown else sections(shown),
        "total": len(entries),
        "chips": chip_rows,
        "group": group,
        "kinds": kinds or {},
        "kind": kind,
        "free": free,
        "free_label": free_label,
        "free_any": any(not e.free for e in entries),
        "sort": sort,
        "sorts": SORTS,
        "filtered": bool(group or kind or free),
        "url": _url(base, {"partia": group, "rodzaj": kind, "sprzet": "0" if free else None, "sort": sort or None}),
        "what": what,
    }
