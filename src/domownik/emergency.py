"""'Na wypadek' – a folder for the family, in case something happens to the parents.

Part of it is written by hand (EmergencyEntry: accounts, where the will is, who should look after the
children…); the rest is gathered from what the app already knows: insurance policies, loans and instalments,
standing payments, regular income, property, contacts and stored documents. The page is printed and handed
over – the app itself is reachable only inside the household's tailnet, so the paper is what the family has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik.domain import recurrence
from domownik.domain.money import format_pln
from domownik.domain.text import fold
from domownik.domain.tree import Tree
from domownik.models import (
    DOC_KINDS,
    EMERGENCY_SECTIONS,
    Attachment,
    Contact,
    EmergencyEntry,
    EmergencyPrint,
    Item,
    RecurringIncome,
    Task,
    User,
)

REVIEW_TITLE = "Przejrzyj „Na wypadek” i wydrukuj nową wersję"
STALE_AFTER = relativedelta(years=1)
PROPERTY_KINDS = ("house", "apartment", "rental", "car")
KEPT_DOCS = ("policy", "contract", "registration")

_DEBT = re.compile(r"kredyt|\brat[aąyę]\b|leasing|lease|pożycz|hipote|\bkarta kredyt", re.I)
# "hasło: …", "PIN=1234", a card number – the folder is printed and handed around
_SECRET = re.compile(
    r"\b(hasło|haslo|password|pin|puk|cvv|cvc)\s*[:=]\s*\S+"
    r"|(?<!\d)(?<!\d )(?<!\d-)\d{4}(?P<sep>[ -]?)\d{4}(?P=sep)\d{4}(?P=sep)\d{4}(?![ -]?\d)",  # a card, not inside an account number
    re.I,
)


def looks_secret(text: str | None) -> bool:
    return bool(text and _SECRET.search(text))


@dataclass
class Row:
    icon: str
    title: str
    where: str = ""
    detail: str = ""
    notes: str = ""
    flag: str = ""  # a short warning shown next to the row ("schodzi samo z konta")


@dataclass
class Overview:
    insurance: list[Row] = field(default_factory=list)
    debts: list[Row] = field(default_factory=list)
    bills: list[Row] = field(default_factory=list)
    income: list[Row] = field(default_factory=list)
    property: list[Row] = field(default_factory=list)
    documents: list[Row] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)


def is_debt(t: Task) -> bool:
    cat = t.budget_category.name if t.budget_category else ""
    return t.category == "payment" and bool(_DEBT.search(t.title) or _DEBT.search(cat))


def _money(t: Task) -> str:
    parts = []
    if t.amount_gr:
        parts.append(format_pln(t.amount_gr))
    if (t.recurrence or {}).get("mode") != "once":
        parts.append(recurrence.describe(t.recurrence))
    return " · ".join(p for p in parts if p and p != "?")


def _row(t: Task, tree: Tree) -> Row:
    flag = "schodzi samo z konta" if t.autopay else ""
    return Row(t.category_icon, t.full_title, tree.label(t.item_id), _money(t), (t.notes or "").strip(), flag)


def overview(db: Session, today: date) -> Overview:
    """What the app already knows, sorted into the sections of the folder."""
    tree = Tree.load(db)
    out = Overview()
    tasks = db.scalars(select(Task).where(Task.active.is_(True)).order_by(Task.item_id, Task.title)).all()
    for t in tasks:
        if t.item_id not in tree.items:
            continue
        repeating = (t.recurrence or {}).get("mode") != "once"
        if t.category == "insurance":
            out.insurance.append(_row(t, tree))
        elif is_debt(t):
            out.debts.append(_row(t, tree))
        elif t.category in ("payment", "tax") and t.direction == "expense" and repeating:
            out.bills.append(_row(t, tree))
    for inc in db.scalars(select(RecurringIncome).order_by(RecurringIncome.sort_order, RecurringIncome.id)):
        if inc.ends_on is None or inc.ends_on >= today:
            where = tree.label(inc.item_id) if inc.item_id in tree.items else ""
            out.income.append(Row("💰", inc.name, where, f"{format_pln(inc.amount_gr)} co miesiąc, ok. {inc.day}."))
    for item in tree.items.values():
        if item.kind in PROPERTY_KINDS:
            out.property.append(Row(item.display_icon, item.name, tree.label(item.id), "", (item.notes or "").strip()))
    out.property.sort(key=lambda r: r.where)
    docs = db.scalars(
        select(Attachment).where(Attachment.doc_kind.in_(KEPT_DOCS), Attachment.item_id.is_not(None))
    ).all()
    for a in sorted(docs, key=lambda a: (tree.label(a.item_id), a.original_name)):
        if a.item_id in tree.items:
            label = a.description or a.original_name
            out.documents.append(Row(DOC_KINDS[a.doc_kind][1], label, tree.label(a.item_id), DOC_KINDS[a.doc_kind][0]))
    out.contacts = list(db.scalars(select(Contact).order_by(Contact.role, Contact.name)))
    return out


def entries(db: Session) -> dict[str, list[EmergencyEntry]]:
    by: dict[str, list[EmergencyEntry]] = {k: [] for k in EMERGENCY_SECTIONS}
    for e in db.scalars(select(EmergencyEntry).order_by(EmergencyEntry.sort_order, EmergencyEntry.id)):
        by.setdefault(e.section, []).append(e)
    return by


def last_print(db: Session) -> EmergencyPrint | None:
    return db.scalar(select(EmergencyPrint).order_by(EmergencyPrint.printed_at.desc()).limit(1))


def changes_since(db: Session, when: datetime) -> list[str]:
    """What was added or changed after the last printout – the paper no longer has it."""
    out = []
    for e in db.scalars(select(EmergencyEntry).where(EmergencyEntry.updated_at > when)):
        out.append(e.title)
    tasks = db.scalars(select(Task).where(Task.active.is_(True), Task.created_at > when)).all()
    out += [t.title for t in tasks if t.category == "insurance" or is_debt(t)]
    out += [c.name for c in db.scalars(select(Contact).where(Contact.created_at > when))]
    return out


@dataclass
class Freshness:
    printed: EmergencyPrint | None
    changes: list[str]
    too_old: bool

    @property
    def needs_print(self) -> bool:
        return self.printed is None or self.too_old or bool(self.changes)


def freshness(db: Session, now: datetime) -> Freshness:
    p = last_print(db)
    if p is None:
        return Freshness(None, [], False)
    return Freshness(p, changes_since(db, p.printed_at), p.printed_at + STALE_AFTER < utc(now))


def utc(now: datetime) -> datetime:
    """Naive UTC – how SQLite's CURRENT_TIMESTAMP (created_at of every row) stores time."""
    return now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now


def local(stored: datetime, tz) -> datetime:
    return stored.replace(tzinfo=UTC).astimezone(tz)


def record_print(db: Session, user: User, now: datetime) -> EmergencyPrint:
    p = EmergencyPrint(printed_at=utc(now), user_id=user.id)
    db.add(p)
    return p


def review_task(db: Session) -> Task | None:
    return db.scalar(select(Task).where(Task.title == REVIEW_TITLE, Task.active.is_(True)).limit(1))


def review_zone(db: Session) -> Item | None:
    """Where the yearly reminder goes: the finance zone if there is one, else the first zone."""
    roots = db.scalars(
        select(Item).where(Item.parent_id.is_(None), Item.archived.is_(False)).order_by(Item.sort_order, Item.id)
    ).all()
    for r in roots:
        if "finans" in fold(r.name):
            return r
    return roots[0] if roots else None


def add_review_task(db: Session, today: date) -> Task | None:
    existing = review_task(db)
    if existing:
        return existing
    zone = review_zone(db)
    if zone is None:
        return None
    t = Task(
        item_id=zone.id,
        title=REVIEW_TITLE,
        category="document",
        recurrence=recurrence.validate({"mode": "interval", "every": 1, "unit": "year", "anchor": "completion"}),
        next_due_date=today + relativedelta(years=1),
        lead_days=14,
        direction="none",
        notes="Sprawdź każdy dział w „Na wypadek” (menu → Na wypadek), popraw co się zmieniło, wydrukuj nową "
        "wersję i podmień kartki u rodziny.",
    )
    db.add(t)
    return t


def counts(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(EmergencyEntry)) or 0


def clean_text(raw, limit: int) -> str:
    return str(raw or "").strip()[:limit]


def touch(e: EmergencyEntry, user: User, now: datetime) -> None:
    e.updated_by_id = user.id
    e.updated_at = utc(now)
