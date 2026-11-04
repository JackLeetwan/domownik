"""Zone tree helpers. Household data is tiny, so the whole tree is loaded into memory."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik.models import Item


@dataclass
class Tree:
    items: dict[int, Item]
    children: dict[int | None, list[int]] = field(default_factory=dict)

    @classmethod
    def load(cls, db: Session, *, include_archived: bool = False) -> Tree:
        """Kept for the rest of the request (db.memoized) – treat the tree as read-only."""
        from domownik.db import memoized

        return memoized(db, ("tree", include_archived), lambda: cls._load(db, include_archived))

    @classmethod
    def _load(cls, db: Session, include_archived: bool) -> Tree:
        stmt = select(Item).order_by(Item.sort_order, Item.name)
        if not include_archived:
            stmt = stmt.where(Item.archived.is_(False))
        items = {i.id: i for i in db.scalars(stmt)}
        children: dict[int | None, list[int]] = {}
        for item in items.values():
            parent = item.parent_id if item.parent_id in items else None
            if item.parent_id is not None and parent is None:
                continue  # parent archived → hide the subtree
            children.setdefault(parent, []).append(item.id)
        return cls(items, children)

    def roots(self) -> list[Item]:
        return [self.items[i] for i in self.children.get(None, [])]

    def kids(self, item_id: int) -> list[Item]:
        return [self.items[i] for i in self.children.get(item_id, [])]

    def subtree_ids(self, item_id: int) -> set[int]:
        out, stack = set(), [item_id]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack.extend(self.children.get(cur, []))
        return out

    def path(self, item_id: int) -> list[Item]:
        """Root → item, for breadcrumbs."""
        out = []
        cur = self.items.get(item_id)
        seen = set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            out.append(cur)
            cur = self.items.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(out))

    def root_of(self, item_id: int) -> Item | None:
        p = self.path(item_id)
        return p[0] if p else None

    def label(self, item_id: int) -> str:
        """'Dom › Ekspres' – zone-qualified name."""
        return " › ".join(i.name for i in self.path(item_id))

    def visible(self) -> set[int]:
        out: set[int] = set()
        for r in self.children.get(None, []):
            out |= self.subtree_ids(r)
        return out
