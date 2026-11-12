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
    BudgetCategory,
    ExternalEvent,
    HealthLink,
    HealthReading,
    Issue,
    Item,
    Medication,
    Supplement,
    Task,
    Transaction,
    User,
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


# ---------------------------------------------------------------- medical profile (Item.attributes of a member)

PROFILE: tuple[tuple[str, str, str], ...] = (
    # attribute key, label, placeholder
    ("blood_type", "Grupa krwi", "np. A Rh+"),
    ("allergies", "Alergie", "leki, pokarmy, użądlenia – albo „brak”"),
    ("chronic", "Choroby przewlekłe", "np. astma, cukrzyca"),
    ("health_notes", "Ważne dla ratownika / lekarza", "implanty, rozrusznik, przebyte operacje…"),
)
BLOOD_TYPES = ("0 Rh+", "0 Rh−", "A Rh+", "A Rh−", "B Rh+", "B Rh−", "AB Rh+", "AB Rh−")


def profile(item: Item) -> dict[str, str]:
    attrs = item.attributes or {}
    return {k: str(attrs[k]) for k, _, _ in PROFILE if attrs.get(k)}


def save_profile(item: Item, form) -> None:
    attrs = dict(item.attributes or {})
    for k, _, _ in PROFILE:
        v = str(form.get(k) or "").strip()[:300]
        if v:
            attrs[k] = v
        else:
            attrs.pop(k, None)
    item.attributes = attrs  # a new dict – JSON columns do not track in-place changes


# ---------------------------------------------------------------- medicine reminders and prescription renewal

RX_LEAD_DAYS = 7


def reminder_of(db: Session, m: Medication, user: User) -> Supplement | None:
    return db.scalar(select(Supplement).where(Supplement.medication_id == m.id, Supplement.user_id == user.id))


def set_reminder(db: Session, m: Medication, user: User, time: str | None, day: date) -> None:
    """ "🔔 Przypominaj mi o 8:00": the medicine becomes one of the person's supplements (push + ✓ in Forma)."""
    s = reminder_of(db, m, user)
    if time is None:
        if s is not None:
            db.delete(s)
        return
    if s is None:
        s = Supplement(user_id=user.id, medication_id=m.id, started_on=day, per_take=1)
        db.add(s)
    s.name = f"💊 {m.name}"[:60]
    s.dose = (m.dose or "")[:40] or None
    s.time = time
    s.active = m.current(day) or (m.started_on is not None and m.started_on > day)


def pause_ended(db: Session, day: date) -> int:
    """Reminders of medicines that ended stop (the scheduler, once a minute – cheap)."""
    n = 0
    for s in db.scalars(select(Supplement).where(Supplement.medication_id.is_not(None), Supplement.active.is_(True))):
        m = db.get(Medication, s.medication_id)
        if m is not None and m.ends_on is not None and m.ends_on < day:
            s.active = False
            n += 1
    return n


def rx_title(m: Medication) -> str:
    return f"Recepta: {m.name} – odnowić"[:200]


def sync_rx_task(db: Session, m: Medication) -> None:
    """A one-off health task on rx_until (a week ahead); cleared date → the open task goes away."""
    t = db.get(Task, m.rx_task_id) if m.rx_task_id else None
    if m.rx_until is None:
        if t is not None and t.active:
            db.delete(t)
        m.rx_task_id = None
        return
    if t is None:
        t = Task(item_id=m.item_id, category=HEALTH, direction="none", lead_days=RX_LEAD_DAYS)
        db.add(t)
    t.title = rx_title(m)
    t.issue_id = m.issue_id
    t.private_to_id = m.private_to_id
    t.recurrence = {"mode": "once", "date": m.rx_until.isoformat()}
    t.next_due_date = m.rx_until
    t.active = True
    t.notes = "Recepta kończy się albo trzeba ją odnowić – umów teleporadę / wizytę. " + (m.prescription or "")
    db.flush()
    m.rx_task_id = t.id


# ---------------------------------------------------------------- readings (blood pressure, sugar…)

READING_KINDS: dict[str, tuple[str, bool]] = {
    # name: (unit, two values)
    "Ciśnienie": ("mmHg", True),
    "Tętno": ("ud./min", False),
    "Glukoza": ("mg/dl", False),
    "Cholesterol całkowity": ("mg/dl", False),
    "LDL": ("mg/dl", False),
    "Temperatura": ("°C", False),
    "Saturacja": ("%", False),
    "Masa ciała": ("kg", False),
}


def fmt(v: float | None) -> str:
    if v is None:
        return ""
    return (f"{v:.1f}".rstrip("0").rstrip(".")).replace(".", ",")


@dataclass
class Series:
    name: str
    unit: str | None
    rows: list[HealthReading]  # newest first

    @property
    def last(self) -> HealthReading:
        return self.rows[0]

    def label(self, r: HealthReading) -> str:
        v = fmt(r.value) + (f"/{fmt(r.value2)}" if r.value2 is not None else "")
        return f"{v} {self.unit or ''}".strip()

    def chart(self) -> dict | None:
        """Up to the last 30 readings as a line (two lines for blood pressure), for an inline SVG."""
        pts = sorted(self.rows[:30], key=lambda r: (r.measured_on, r.id))
        if len(pts) < 2:
            return None
        vals = [r.value for r in pts] + [r.value2 for r in pts if r.value2 is not None]
        lo, hi = min(vals), max(vals)
        pad = max(0.5, (hi - lo) * 0.1)
        lo, hi = lo - pad, hi + pad
        w, h = 300, 80
        first, last = pts[0].measured_on, pts[-1].measured_on
        span = max(1, (last - first).days)

        def xy(r: HealthReading, v: float) -> str:
            x = 4 + (r.measured_on - first).days / span * (w - 8)
            return f"{x:.1f},{4 + (hi - v) / (hi - lo) * (h - 8):.1f}"

        return {
            "w": w,
            "h": h,
            "line": " ".join(xy(r, r.value) for r in pts),
            "line2": " ".join(xy(r, r.value2) for r in pts if r.value2 is not None),
            "from": first,
            "to": last,
        }


def readings(db: Session, item_id: int) -> list[Series]:
    rows = db.scalars(
        select(HealthReading)
        .where(HealthReading.item_id == item_id)
        .order_by(HealthReading.measured_on.desc(), HealthReading.id.desc())
    ).all()
    by: dict[str, list[HealthReading]] = {}
    for r in rows:
        by.setdefault(r.name, []).append(r)
    return sorted(
        (Series(n, rs[0].unit, rs) for n, rs in by.items()), key=lambda s: s.rows[0].measured_on, reverse=True
    )


def parse_number(raw) -> float | None:
    v = str(raw or "").strip().replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ---------------------------------------------------------------- spending (budget category "Zdrowie")

BUDGET_NAME = "zdrowie"


def spending(db: Session, tree: Tree, people: list[Item], year: int) -> dict:
    """Health spending of the year: everything booked in the "Zdrowie" budget category (and its children),
    per person when the booking is tied to them (or to something under them)."""
    roots = [c.id for c in db.scalars(select(BudgetCategory)) if c.name.strip().lower() == BUDGET_NAME]
    if not roots:
        return {"total": 0, "by_person": {}, "recent": {}, "found": False}
    ids = set(roots) | {c.id for c in db.scalars(select(BudgetCategory).where(BudgetCategory.parent_id.in_(roots)))}
    rows = db.scalars(
        select(Transaction)
        .where(
            Transaction.budget_category_id.in_(ids),
            Transaction.booked_on >= date(year, 1, 1),
            Transaction.booked_on <= date(year, 12, 31),
        )
        .order_by(Transaction.booked_on.desc(), Transaction.id.desc())
        .execution_options(all_private=True)
    ).all()
    total = -sum(t.amount_gr for t in rows)
    by_person: dict[int, int] = {}
    recent: dict[int, list[Transaction]] = {}
    for t in rows:
        who = member_of(tree, t.item_id, people)
        if who is not None:
            by_person[who.id] = by_person.get(who.id, 0) - t.amount_gr
            recent.setdefault(who.id, []).append(t)
    return {"total": total, "by_person": by_person, "recent": recent, "found": True}
