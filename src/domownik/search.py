"""Search box: everything the household wrote down, matched in Python (the data is tiny).

Every word of the query has to occur somewhere in a record; case and Polish diacritics are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domownik import catalog
from domownik.domain.text import fold, snippet, terms
from domownik.domain.tree import Tree
from domownik.models import Attachment, BoardNote, Completion, Issue, JournalEntry, Task, Todo, Warranty

# group key → (heading, icon), in display order
KINDS: dict[str, tuple[str, str]] = {
    "item": ("Strefy i elementy", "📦"),
    "task": ("Zadania", "📋"),
    "doc": ("Dokumenty i gwarancje", "📎"),
    "todo": ("Do zrobienia", "☐"),
    "issue": ("Usterki i sprawy", "⚠️"),
    "journal": ("Dziennik", "📓"),
    "note": ("Notatki", "📝"),
    "done": ("Historia wykonań", "✅"),
}
MIN_QUERY = 2  # shorter queries would match half the house


def _d(d) -> str:
    return f"{d.day}.{d.month:02}.{d.year}"


@dataclass
class Hit:
    kind: str
    icon: str
    title: Markup
    path: str
    detail: Markup | None = None
    url: str | None = None  # a page to go to …
    task_id: int | None = None  # … or the task window to open
    sort: Any = None


@dataclass
class Results:
    query: str
    groups: dict[str, list[Hit]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.groups.values())


def _attr_labels() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for tpl in catalog.templates().values():
        for a in tpl.attributes:
            out.setdefault(tpl.kind, {}).setdefault(a["key"], a["label"])
    return out


def _hit(kind: str, words: list[str], title: str, fields: list[str], **kw) -> Hit | None:
    """A hit when all words occur in title + fields together; the detail is the field matching most words."""
    texts = [title, *[f for f in fields if f]]
    folded = [fold(t) for t in texts]
    blob = "\n".join(folded)
    if not all(w in blob for w in words):
        return None
    best, best_n = None, 0
    for t, f in zip(texts[1:], folded[1:], strict=True):
        n = sum(w in f for w in words)
        if n > best_n:
            best, best_n = t, n
    detail = snippet(best, words) if best is not None else None
    return Hit(kind=kind, icon=kw.pop("icon", KINDS[kind][1]), title=snippet(title, words, 120), detail=detail, **kw)


def search(db: Session, query: str) -> Results:
    res = Results(query=query.strip())
    words = terms(query)
    if not words or len("".join(words)) < MIN_QUERY:
        return res
    tree = Tree.load(db)
    visible = tree.visible()
    groups: dict[str, list[Hit]] = {k: [] for k in KINDS}
    labels = _attr_labels()

    def path_of(item_id: int | None) -> str:
        return tree.label(item_id) if item_id in visible else ""

    for item in tree.items.values():
        if item.id not in visible:
            continue
        attrs = [
            f"{labels.get(item.kind, {}).get(k, k)}: {v}"
            for k, v in (item.attributes or {}).items()
            if not str(k).startswith("_") and v not in (None, "")
        ]
        parent = tree.label(item.parent_id) if item.parent_id in visible else ""
        h = _hit(
            "item",
            words,
            item.name,
            [*attrs, item.notes or ""],
            icon=item.display_icon,
            path=parent,
            url=f"/items/{item.id}",
            sort=tree.label(item.id),
        )
        if h:
            groups["item"].append(h)

    tasks = db.scalars(select(Task).where(Task.item_id.in_(visible)))
    for t in tasks:
        h = _hit(
            "task",
            words,
            t.title,
            [t.notes or ""],
            icon=t.category_icon,
            path=path_of(t.item_id) + ("" if t.active else " · zakończone"),
            task_id=t.id,
            sort=(not t.active, t.next_due_date or t.created_at.date(), t.title),
        )
        if h:
            groups["task"].append(h)

    for a in db.scalars(select(Attachment).where(Attachment.item_id.in_(visible))):
        h = _hit(
            "doc",
            words,
            a.original_name,
            [a.description or "", a.kind_label],
            icon=a.icon,
            path=f"{path_of(a.item_id)} · {a.kind_label} · {_d(a.created_at.date())}",
            url=f"/files/{a.id}/view",
            sort=(0, -a.created_at.timestamp()),
        )
        if h:
            groups["doc"].append(h)

    for w in db.scalars(select(Warranty).where(Warranty.item_id.in_(visible))):
        until = f" · do {_d(w.ends_on)}" if w.ends_on else ""
        h = _hit(
            "doc",
            words,
            w.name,
            [w.notes or "", "Gwarancja / umowa"],
            icon="📄",
            path=path_of(w.item_id) + until,
            url=f"/items/{w.item_id}#dokumenty",
            sort=(0, -w.created_at.timestamp()),
        )
        if h:
            groups["doc"].append(h)

    for td in db.scalars(select(Todo)):
        if td.item_id is not None and td.item_id not in visible:
            continue
        where = path_of(td.item_id) if td.item_id else "Ogólne"
        h = _hit(
            "todo",
            words,
            td.title,
            [td.notes or ""],
            path=where + (" · zrobione" if td.done_on else ""),
            url=f"/items/{td.item_id}" if td.item_id else "/do-zrobienia",
            sort=(td.done_on is not None, td.id),
        )
        if h:
            groups["todo"].append(h)

    for iss in db.scalars(select(Issue).where(Issue.item_id.in_(visible))):
        state = f" · od {_d(iss.started_on)}" + (" · zakończone" if iss.resolved_on else "")
        h = _hit(
            "issue",
            words,
            iss.title,
            [iss.notes or ""],
            path=path_of(iss.item_id) + state,
            url=f"/items/{iss.item_id}",
            sort=(iss.resolved_on is not None, -iss.started_on.toordinal()),
        )
        if h:
            groups["issue"].append(h)

    for j in db.scalars(select(JournalEntry).where(JournalEntry.item_id.in_(visible))):
        first = j.body.strip().splitlines()[0] if j.body.strip() else ""
        h = _hit(
            "journal",
            words,
            _d(j.noted_on),
            [j.body],
            path=path_of(j.item_id),
            url=f"/items/{j.item_id}",
            sort=-j.noted_on.toordinal(),
        )
        if h:
            if h.detail is None:
                h.detail = snippet(first, words)
            groups["journal"].append(h)

    for n in db.scalars(select(BoardNote)):
        h = _hit(
            "note",
            words,
            n.body,
            [],
            icon="📌" if n.pinned else "📝",
            path="w archiwum" if n.archived else "tablica",
            url="/notatki",
            sort=(n.archived, -(n.updated_at or n.created_at).timestamp()),
        )
        if h:
            groups["note"].append(h)

    stmt = (
        select(Completion)
        .join(Task)
        .where(Task.item_id.in_(visible))
        .options(selectinload(Completion.task), selectinload(Completion.performed_by))
    )
    for c in db.scalars(stmt):
        fields = [c.notes or "", f"Wykonawca: {c.vendor}" if c.vendor else ""]
        if c.performed_by is not None:
            fields.append(f"Zrobił(a): {c.performed_by.name}")
        if not any(fields):
            continue
        # the task title gives context but alone does not make a history hit (that is the task itself)
        h = _hit(
            "done",
            words,
            c.task.title,
            fields,
            icon=c.task.category_icon,
            path=f"{path_of(c.task.item_id)} · {_d(c.done_on)}",
            task_id=c.task_id,
            sort=-c.done_on.toordinal(),
        )
        if h and h.detail is not None:
            groups["done"].append(h)

    for k, hits in groups.items():
        if hits:
            hits.sort(key=lambda h: h.sort)
            res.groups[k] = hits
    return res
