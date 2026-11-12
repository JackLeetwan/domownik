"""🩺 Zdrowie: one place per household member for what already lives around the app – health tasks (visits,
check-ups, vaccinations) wherever they were added, health issues, medical records – plus medicines and links
to records kept elsewhere. Nothing is copied: a visit added here is a normal task on the person's item.

Who a health task belongs to: the member item it sits on (or under), else the person of the issue it is part
of, else the member linked to its assignee's account. The rest ("Wizyta kontrolna" on the house) is "Inne".
External calendars have no categories – events that look like a doctor's visit are picked by words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import documents, extcal, services
from domownik.domain.tree import Tree
from domownik.models import (
    MEDICAL_DOC_KINDS,
    MEMBER_KINDS,
    Attachment,
    ExternalEvent,
    HealthLink,
    Issue,
    Item,
    Medication,
    Task,
)

HEALTH = "health"
AHEAD_DAYS = 60
# words of a doctor's visit in an external calendar's event title (lower case, word starts)
_VISIT_WORDS = re.compile(
    r"\b(lekarz|lekarsk|dentyst|stomatolog|ortodont|ortoped|pediatr|internist|kardiolog|dermatolog|okulist|"
    r"laryngolog|ginekolog|urolog|neurolog|alergolog|endokrynolog|psycholog|psychiatr|fizjoterap|rehabilit|"
    r"wizyta|szczepien|badani|usg|rtg|rezonans|morfologi|przychodni|luxmed|lux med|medicover|enel|"
    r"zabieg|kontrola po|dr\.? )",
    re.IGNORECASE,
)


def looks_medical(title: str) -> bool:
    return bool(_VISIT_WORDS.search(title or ""))


def members(db: Session, tree: Tree) -> list[Item]:
    """Household members (adults, then children), not archived."""
    rows = [it for it in tree.items.values() if it.kind in MEMBER_KINDS and not it.archived]
    return sorted(rows, key=lambda it: (it.kind != "member", it.sort_order, it.name))


def member_of(tree: Tree, item_id: int | None, people: list[Item]) -> Item | None:
    """The member whose subtree holds item_id (the item itself or something under it)."""
    if item_id is None:
        return None
    ids = {p.id: p for p in people}
    for it in reversed(tree.path(item_id)):
        if it.id in ids:
            return ids[it.id]
    return None


@dataclass
class Overview:
    rows_by_person: dict[int, list[services.TaskRow]] = field(default_factory=dict)
    other_rows: list[services.TaskRow] = field(default_factory=list)
    calendar: list[tuple[date, ExternalEvent]] = field(default_factory=list)


def health_rows(db: Session, tree: Tree, day: date, people: list[Item]) -> Overview:
    """Every active health task of the household, sorted to the person it is about."""
    out = Overview(rows_by_person={p.id: [] for p in people})
    by_user = {p.user_id: p for p in people if p.user_id}
    issue_person = {
        i.id: i.item_id for i in db.scalars(select(Issue).where(Issue.item_id.in_([p.id for p in people] or [0])))
    }
    for r in services.task_rows(db, tree, day):
        t = r.task
        if t.category != HEALTH and not (t.issue_id and t.issue_id in issue_person):
            continue
        who = member_of(tree, t.item_id, people)
        if who is None and t.issue_id in issue_person:
            who = next((p for p in people if p.id == issue_person[t.issue_id]), None)
        if who is None and t.assignee_id in by_user:
            who = by_user[t.assignee_id]
        (out.rows_by_person[who.id] if who else out.other_rows).append(r)
    return out


def calendar_visits(db: Session, day: date) -> list[tuple[date, ExternalEvent]]:
    """Doctor-like events of the external calendars the viewer sees, from today on."""
    found = extcal.events_by_day(db, day, day + timedelta(days=AHEAD_DAYS))
    seen: set[int] = set()
    out = []
    for d in sorted(found):
        for ev in found[d]:
            if ev.id not in seen and looks_medical(ev.title):
                seen.add(ev.id)
                out.append((d, ev))
    return out


def medications(db: Session, item_id: int) -> list[Medication]:
    rows = db.scalars(select(Medication).where(Medication.item_id == item_id)).all()
    return [m for m in rows if issue_visible(db, m.issue_id)]


def split_meds(meds: list[Medication], day: date) -> tuple[list[Medication], list[Medication]]:
    now = sorted((m for m in meds if m.current(day)), key=lambda m: m.name.lower())
    past = sorted(
        (m for m in meds if not m.current(day)),
        key=lambda m: m.ends_on or m.started_on or date.min,
        reverse=True,
    )
    return now, past


def issue_visible(db: Session, issue_id: int | None) -> bool:
    """A thing filed under someone else's private issue is as hidden as the issue."""
    return issue_id is None or db.get(Issue, issue_id) is not None


def medical_files(db: Session, item: Item) -> list[Attachment]:
    """Medical records of a person: the medical kinds, or anything filed under one of their issues."""
    return [
        a
        for a in item.attachments
        if (a.doc_kind in MEDICAL_DOC_KINDS or a.issue_id is not None) and documents.visible(db, a)
    ]


def links(db: Session, item_id: int, issue_id: int | None = None) -> list[HealthLink]:
    q = select(HealthLink).where(HealthLink.item_id == item_id).order_by(HealthLink.title)
    if issue_id is not None:
        q = q.where(HealthLink.issue_id == issue_id)
    return [h for h in db.scalars(q) if issue_visible(db, h.issue_id)]


def issue_tasks(db: Session, issue_id: int) -> list[Task]:
    return list(db.scalars(select(Task).where(Task.issue_id == issue_id).order_by(Task.next_due_date)))


def clean_url(raw: str | None) -> str | None:
    """Only http(s) links – never javascript: or data: in an href."""
    url = (raw or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        if re.match(r"^[a-z][a-z0-9+.-]*:(?!\d)", url, re.IGNORECASE):  # javascript:, data:, mailto:…
            return None
        url = "https://" + url
    return url[:1000] if re.match(r"^https?://[^\s/]+", url, re.IGNORECASE) else None
