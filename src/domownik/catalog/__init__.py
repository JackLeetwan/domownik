"""Templates of items with suggested tasks, and seed data (zones, budget categories)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from domownik.domain import recurrence

HERE = Path(__file__).parent


@dataclass
class TaskTemplate:
    title: str
    category: str
    recurrence: dict[str, Any]
    lead_days: int = 7
    amount: float | None = None
    direction: str = "expense"
    budget: str | None = None
    default: bool = True
    ask_date: bool = False
    hint: str | None = None

    @property
    def needs_user_date(self) -> bool:
        """Fixed date chosen by the user (birthday) – encoded as dates: ['?']."""
        return self.recurrence.get("mode") == "fixed" and self.recurrence.get("dates") == ["?"]

    def describe(self) -> str:
        if self.needs_user_date:
            return "co roku, w wybranym dniu"
        return recurrence.describe(self.recurrence)


@dataclass
class ItemTemplate:
    key: str
    name: str
    kind: str
    icon: str
    order: int = 100
    attributes: list[dict[str, str]] = field(default_factory=list)
    tasks: list[TaskTemplate] = field(default_factory=list)


@lru_cache
def templates() -> dict[str, ItemTemplate]:
    out: dict[str, ItemTemplate] = {}
    for path in sorted((HERE / "templates").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        tasks = []
        for t in raw.get("tasks") or []:
            tt = TaskTemplate(**t)
            if not tt.needs_user_date:
                recurrence.validate(tt.recurrence)  # fail fast on a broken template
            if tt.amount is None and "direction" not in t:
                tt.direction = "none"
            tasks.append(tt)
        raw["tasks"] = tasks
        tpl = ItemTemplate(**raw)
        out[tpl.key] = tpl
    return dict(sorted(out.items(), key=lambda kv: (kv[1].order, kv[1].name)))


def seed(name: str) -> Any:
    return yaml.safe_load((HERE / "seed" / f"{name}.yaml").read_text(encoding="utf-8"))


# Quick-add suggestions for items that were not created from zones.yaml
KIND_SUGGESTIONS: dict[str, list[str]] = {
    "house": ["heat_pump", "coffee_machine", "washing_machine", "dryer", "dishwasher", "generic_appliance"],
    "apartment": ["coffee_machine", "washing_machine", "dryer", "dishwasher", "generic_appliance"],
    "rental": ["washing_machine", "dishwasher", "generic_appliance"],
    "child": ["kindergarten"],
}


def suggestions(item_kind: str, item_name: str, attributes: dict[str, Any]) -> list[ItemTemplate]:
    """Templates offered as '＋ …' tiles inside an item: own setting, default zone with that name, kind default."""
    keys = attributes.get("_suggest")
    if not keys:
        zone = next((z for z in seed("zones") if z["name"].lower() == item_name.lower()), None)
        keys = zone.get("suggest") if zone else None
    if not keys:
        keys = KIND_SUGGESTIONS.get(item_kind, [])
    tpls = templates()
    return [tpls[k] for k in keys if k in tpls]
