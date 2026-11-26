"""The "Dotyczy" picker: the most used items as big chips, the rest folded by area, with a search.

A native <select> of every item in alphabetical order put "Dom › …" appliances first and pushed the
people, cars and finances below the fold. Used by the quick add window and the task form.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik.domain.tree import Tree
from domownik.models import Item, Task

TOP = 8
OTHER = "Pozostałe"


@dataclass
class Pick:
    top: list[Item]
    groups: list[tuple[str, list[Item]]] = field(default_factory=list)
    current: int | None = None
    label: dict[int, str] = field(default_factory=dict)

    @property
    def rest_count(self) -> int:
        return sum(len(g) for _, g in self.groups)

    @property
    def current_folded(self) -> bool:
        return self.current is not None and all(i.id != self.current for i in self.top)


def build(db: Session, tree: Tree, current: int | None = None) -> Pick:
    """Top: the items with the most tasks (and the current one); the rest grouped under their area."""
    items = list(tree.items.values())
    if not items:
        return Pick(top=[], current=current)
    used = dict(db.execute(select(Task.item_id, func.count(Task.id)).group_by(Task.item_id)).all())
    by_use = sorted(items, key=lambda i: (-used.get(i.id, 0), tree.label(i.id).lower()))
    top = [i for i in by_use if used.get(i.id)][:TOP] or by_use[:TOP]
    if current in tree.items and all(i.id != current for i in top):
        top.append(tree.items[current])
    top_ids = {i.id for i in top}

    areas: dict[str, list[Item]] = {}
    for i in sorted(items, key=lambda i: tree.label(i.id).lower()):
        if i.id in top_ids:
            continue
        root = tree.root_of(i.id) or i
        areas.setdefault(root.name, []).append(i)
    groups, other = [], []
    for area, its in sorted(areas.items(), key=lambda a: a[0].lower()):
        if len(its) == 1 and its[0].parent_id is None:
            other.extend(its)  # a lone top-level item (a person, the rented flat) – no heading of its own
        else:
            groups.append((area, its))
    if other:
        groups.append((OTHER, other))
    return Pick(top=top, groups=groups, current=current, label={i.id: tree.label(i.id) for i in items})
