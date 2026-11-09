"""Bank statement import (CSV from the bank, mBank first): read the lines, propose what each one is, book it.

A line is proposed as one of:
- ``dup``  – already in the budget (entered by hand, "Zakończ zakupy", autopay) – nothing new is booked;
- ``task`` – the payment of a task with that amount due around that day (rata, OC…) – the task is ticked off;
- ``inc``  – a monthly income expected that month – confirmed ("✓ Wpłynęło");
- ``cat``  – an ordinary expense/income in a category, from a learned rule ("biedronka" → Jedzenie);
- ``skip`` – not for the budget (own transfers, unknown lines nobody picked a category for).

Every line looked at is remembered (BankLine), so importing an overlapping statement again offers only new ones.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import budget
from domownik.models import BankLine, BankRule, BudgetCategory, RecurringIncome, Task, Transaction

MAX_LINES = 5000  # a year of one account is ~1100 lines
# mBank marks a card payment not settled yet; the next statement shows the same payment without it
PENDING = re.compile(r"\s*transakcja nierozliczona\s*", re.I)

# ---------------------------------------------------------------- parsing


@dataclass
class Line:
    booked_on: date
    amount_gr: int  # + money in, − money out
    description: str
    bank_category: str = ""
    ref: str = ""


class StatementError(ValueError):
    pass


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _norm_head(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.strip().lstrip("#").strip().lower())


DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y.%m.%d", "%d/%m/%Y")


def parse_date(raw: str) -> date | None:
    raw = raw.strip()[:10]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(raw: str) -> int | None:
    """'-1 234,56 PLN' / '1234.56' / '−54,90' → grosze (signed)."""
    s = raw.replace("−", "-").replace("\xa0", "").replace(" ", "")
    s = re.sub(r"[A-Za-z]+$", "", s).strip()
    if not s:
        return None
    if "," in s and "." in s:  # 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    if not re.fullmatch(r"[+-]?\d+(\.\d{1,2})?", s):
        return None
    sign = -1 if s.startswith("-") else 1
    whole, _, frac = s.lstrip("+-").partition(".")
    return sign * (int(whole) * 100 + int((frac + "00")[:2]))


def _find(heads: list[str], *names: str, exclude: tuple[str, ...] = ()) -> int | None:
    for name in names:
        for i, h in enumerate(heads):
            if h == name:
                return i
    for name in names:
        for i, h in enumerate(heads):
            if h.startswith(name) and not any(x in h for x in exclude):
                return i
    return None


def parse_statement(raw: bytes) -> list[Line]:
    """Lines of a bank CSV. Finds the header row itself (mBank puts account info above it)."""
    text = _decode(raw)
    sample = "\n".join(text.splitlines()[:60])
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    if "\x00" in text[:4096]:  # a photo / PDF / Excel file picked by mistake
        raise StatementError("To nie jest plik CSV. Wyeksportuj historię z banku jako CSV.")
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    except csv.Error as exc:
        raise StatementError("Nie udało się odczytać pliku CSV. Wyeksportuj historię z banku jeszcze raz.") from exc
    head_at = None
    for i, row in enumerate(rows[:80]):
        heads = [_norm_head(c) for c in row]
        if any(h.startswith("data") for h in heads) and any(h.startswith("kwota") for h in heads):
            head_at = i
            break
    if head_at is None:
        raise StatementError("Nie widzę nagłówka z kolumnami „Data” i „Kwota”. Wyeksportuj historię jako CSV.")
    heads = [_norm_head(c) for c in rows[head_at]]
    c_date = _find(heads, "data operacji", "data transakcji", "data księgowania", "data")
    c_amount = _find(heads, "kwota", "kwota operacji", exclude=("saldo",))
    c_cat = _find(heads, "kategoria")
    c_desc = [
        i
        for i in (
            _find(heads, "opis operacji", "opis"),
            _find(heads, "tytuł"),
            _find(heads, "nadawca/odbiorca", "odbiorca/nadawca", "kontrahent", "odbiorca", "nadawca"),
        )
        if i is not None
    ]
    if c_date is None or c_amount is None:
        raise StatementError("Brakuje kolumny z datą albo kwotą.")
    out: list[Line] = []
    seen: dict[str, int] = {}
    for row in rows[head_at + 1 :]:
        if len(row) <= max(c_date, c_amount):
            continue
        d = parse_date(row[c_date])
        amount = parse_amount(row[c_amount])
        if d is None or not amount:
            continue
        parts = []
        for i in c_desc:
            part = re.sub(r"\s+", " ", PENDING.sub(" ", row[i]).strip().strip("'\"")) if i < len(row) else ""
            if part and part not in parts:
                parts.append(part)
        desc = " · ".join(parts)[:300] or "(bez opisu)"
        line = Line(d, amount, desc, row[c_cat].strip() if c_cat is not None and c_cat < len(row) else "")
        base = f"{d.isoformat()}|{amount}|{desc.lower()}"
        n = seen.get(base, 0)  # two identical coffees on one day are two lines
        seen[base] = n + 1
        line.ref = hashlib.sha1(f"{base}|{n}".encode()).hexdigest()[:32]
        out.append(line)
        if len(out) >= MAX_LINES:
            break
    if not out:
        raise StatementError("W pliku nie ma żadnych operacji.")
    return out


# ---------------------------------------------------------------- what is it?

# words of the bank, not of the shop – never a rule
NOISE = (
    "zakup przy użyciu karty",
    "zakup przy uzyciu karty",
    "płatność kartą",
    "platnosc karta",
    "płatność web blik",
    "płatność blik",
    "przelew wychodzący",
    "przelew przychodzący",
    "przelew zewnętrzny",
    "przelew na telefon",
    "przelew mtransfer",
    "polecenie zapłaty",
    "zlecenie stałe",
    "transakcja kartą",
    "blik",
    "pln",
    "eur",
)
TASK_DAYS_EARLY, TASK_DAYS_LATE = 10, 20  # a payment this many days before / after the task's due date
OWN_TRANSFER = ("przelew własny", "przelew wlasny", "przelew wewnętrzny", "przelew między rachunkami")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def merchant_key(description: str) -> str:
    """'ZAKUP PRZY UŻYCIU KARTY · BIEDRONKA 1234 WARSZAWA' → 'biedronka' – the word a rule remembers."""
    text = description.lower()
    for phrase in NOISE:  # whole words only – "eur" must not cut "EURO-NET" into "o net"
        text = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", text)
    words = [w for w in normalize(text).split() if len(w) >= 3 and not any(ch.isdigit() for ch in w)]
    return " ".join(words[:2])[:80]


@dataclass
class Proposal:
    line: Line
    choice: str  # "dup:<tx>", "task:<id>", "inc:<id>", "cat:<id>", "skip"
    why: str = ""
    learned: bool = False  # the category came from a rule (no need to learn it again)


@dataclass
class Preview:
    proposals: list[Proposal] = field(default_factory=list)
    already: int = 0  # lines imported before
    tasks: dict[int, Task] = field(default_factory=dict)
    incomes: dict[int, RecurringIncome] = field(default_factory=dict)
    duplicates: dict[int, Transaction] = field(default_factory=dict)


def _close(a: int, b: int) -> bool:
    return abs(abs(a) - abs(b)) <= max(100, abs(b) * 3 // 100)


def _rule_category(rules: list[BankRule], desc: str) -> BankRule | None:
    text = normalize(desc)
    hits = [r for r in rules if r.pattern and r.pattern in text]
    return max(hits, key=lambda r: len(r.pattern)) if hits else None


# the bank's category (mBank) → the name of a seeded category of ours
BANK_ALIASES = {
    "żywność": "Jedzenie",
    "jedzenie": "Jedzenie",
    "restauracje": "Restauracje",
    "kawiarnie": "Restauracje",
    "paliwo": "Paliwo",
    "zdrowie": "Zdrowie",
    "apteka": "Zdrowie",
    "rozrywka": "Rozrywka",
    "kultura": "Rozrywka",
    "multimedia": "Subskrypcje",
    "telefon": "Internet i telefon",
    "internet": "Internet i telefon",
    "prąd": "Media",
    "gaz": "Media",
    "media": "Media",
    "ubezpieczen": "Ubezpieczenia",
    "podatk": "Podatki",
    "prezent": "Prezenty i okazje",
    "dzieci": "Dziecko – ubrania",
    "wynagrodzenie": "Pensje",
}
CARD_WORDS = ("karty", "kartą", "karta", "blik")


def _bank_category(cats: list[BudgetCategory], name: str, income: bool) -> BudgetCategory | None:
    """The bank's own category ("Żywność i chemia domowa") when it names one of ours."""
    name = name.lower()
    if not name:
        return None
    by_name = {c.name: c for c in cats if c.is_income == income}
    for word, ours in BANK_ALIASES.items():
        if word in name and ours in by_name:
            return by_name[ours]
    for c in cats:
        if c.is_income == income and len(c.name) >= 4 and (c.name.lower() in name or name in c.name.lower()):
            return c
    return None


def _task_score(t: Task, words: set[str], ln: Line) -> tuple[int, int, int]:
    """Best payment for a line: a word of the title in the description, then the closest amount and day."""
    common = len(words & {w for w in normalize(t.title).split() if len(w) >= 3})
    return (-common, abs(abs(t.amount_gr or 0) - abs(ln.amount_gr)), abs((ln.booked_on - t.next_due_date).days))


def propose(db: Session, lines: list[Line], today: date) -> Preview:
    out = Preview()
    known = set(db.scalars(select(BankLine.ref).where(BankLine.ref.in_([ln.ref for ln in lines]))))
    fresh = [ln for ln in lines if ln.ref not in known]
    out.already = len(lines) - len(fresh)
    if not fresh:
        return out
    first = min(ln.booked_on for ln in fresh) - timedelta(days=31)
    last = max(ln.booked_on for ln in fresh) + timedelta(days=31)
    # what is already booked and did not come from a statement
    from_bank = set(db.scalars(select(BankLine.transaction_id).where(BankLine.transaction_id.is_not(None))))
    booked = [
        tx
        for tx in db.scalars(
            select(Transaction).where(Transaction.booked_on.between(first, last)).execution_options(all_private=True)
        )
        if tx.id not in from_bank
    ]
    tasks = list(
        db.scalars(
            select(Task)
            .where(
                Task.active.is_(True),
                Task.amount_gr.is_not(None),
                Task.direction != "none",
                Task.next_due_date.between(first, last),
            )
            .execution_options(all_private=True)
        )
    )
    incomes = list(db.scalars(select(RecurringIncome)))
    rules = list(db.scalars(select(BankRule)))
    cats = budget.active_categories(db)
    used_tx: set[int] = set()
    used_task: set[int] = set()
    used_inc: set[tuple[int, int, int]] = set()

    for ln in sorted(fresh, key=lambda x: x.booked_on):
        p = Proposal(ln, "skip")
        income = ln.amount_gr > 0
        # 1. already in the budget: same amount, a few days apart
        dup = min(
            (
                tx
                for tx in booked
                if tx.id not in used_tx
                and (tx.amount_gr > 0) == income
                and abs(tx.amount_gr) == abs(ln.amount_gr)
                and abs((tx.booked_on - ln.booked_on).days) <= 4
            ),
            key=lambda tx: abs((tx.booked_on - ln.booked_on).days),
            default=None,
        )
        if dup is not None:
            used_tx.add(dup.id)
            out.duplicates[dup.id] = dup
            p.choice, p.why = f"dup:{dup.id}", "już jest w budżecie"
            out.proposals.append(p)
            continue
        if any(x in ln.description.lower() for x in OWN_TRANSFER):
            p.why = "przelew między własnymi kontami"
            out.proposals.append(p)
            continue
        # 2. a planned payment / income of that amount around that day
        if not income:
            cands = [
                t
                for t in tasks
                if t.id not in used_task
                and t.direction == "expense"
                and _close(t.amount_gr or 0, ln.amount_gr)
                and -TASK_DAYS_EARLY <= (ln.booked_on - t.next_due_date).days <= TASK_DAYS_LATE
            ]
            match: Task | None = None
            if cands:
                words = set(normalize(ln.description).split())
                match = min(cands, key=lambda t, w=words, x=ln: _task_score(t, w, x))
                card = any(w in ln.description.lower() for w in CARD_WORDS)
                if card and _task_score(match, words, ln)[0] == 0:
                    match = None  # a card purchase of the same amount (fuel for 260 zł) is not a bill
            if match is not None:
                used_task.add(match.id)
                out.tasks[match.id] = match
                p.choice, p.why = f"task:{match.id}", "płatność z harmonogramu"
                out.proposals.append(p)
                continue
        else:
            first_m, last_m = budget.month_bounds(ln.booked_on.year, ln.booked_on.month)
            confirmed = set(
                db.scalars(
                    select(Transaction.income_id).where(
                        Transaction.income_id.is_not(None), Transaction.booked_on.between(first_m, last_m)
                    )
                )
            )
            inc = next(
                (
                    i
                    for i in incomes
                    if i.expected_in(first_m, last_m)
                    and i.id not in confirmed
                    and (i.id, ln.booked_on.year, ln.booked_on.month) not in used_inc
                    and abs(ln.amount_gr - i.amount_gr) <= max(100, i.amount_gr // 10)
                ),
                None,
            )
            if inc is not None:
                used_inc.add((inc.id, ln.booked_on.year, ln.booked_on.month))
                out.incomes[inc.id] = inc
                p.choice, p.why = f"inc:{inc.id}", "dochód co miesiąc"
                out.proposals.append(p)
                continue
        # 3. a learned rule, then the bank's own category name
        rule = _rule_category(rules, ln.description)
        if rule is not None and rule.budget_category.is_income == income and not rule.budget_category.archived:
            p.choice, p.why, p.learned = f"cat:{rule.budget_category_id}", f"reguła „{rule.pattern}”", True
        else:
            cat = _bank_category(cats, ln.bank_category, income)
            if cat is not None:
                p.choice, p.why = f"cat:{cat.id}", f"kategoria banku „{ln.bank_category}”"
            else:
                p.why = "wybierz kategorię"
        out.proposals.append(p)
    return out


# ---------------------------------------------------------------- booking


@dataclass
class Result:
    booked: int = 0
    paid: int = 0
    incomes: int = 0
    linked: int = 0
    skipped: int = 0
    rules: int = 0


def learn(db: Session, description: str, category_id: int) -> bool:
    key = merchant_key(description)
    if len(key) < 3:
        return False
    rule = db.scalar(select(BankRule).where(BankRule.pattern == key))
    if rule is None:
        db.add(BankRule(pattern=key, budget_category_id=category_id))
        return True
    if rule.budget_category_id != category_id:
        rule.budget_category_id = category_id
        return True
    return False


def book(db: Session, lines: list[tuple[Line, str, bool]], *, user, today: date) -> Result:
    """lines: (line, choice, learn?) – choice as in Proposal. Commits."""
    from domownik.services import complete_task

    res = Result()
    for ln, choice, wants_rule in lines:
        if db.get(BankLine, ln.ref) is not None:
            continue  # imported twice in parallel
        kind, _, raw_id = choice.partition(":")
        obj_id = int(raw_id) if raw_id.isdigit() else None
        tx: Transaction | None = None
        if kind == "dup" and obj_id:
            tx = db.get(Transaction, obj_id)
            res.linked += tx is not None
        elif kind == "task" and obj_id:
            task = db.get(Task, obj_id)
            # the preview may be stale: ticked off by hand meanwhile → the task moved on, do not pay it twice
            due = task.next_due_date if task is not None and task.active else None
            if due is not None and -TASK_DAYS_EARLY <= (ln.booked_on - due).days <= TASK_DAYS_LATE:
                comp = complete_task(
                    db,
                    task,
                    user=user,
                    done_on=ln.booked_on,
                    amount_gr=abs(ln.amount_gr),
                    km=None,
                    vendor=None,
                    notes="Z wyciągu bankowego",
                )
                tx = db.scalar(select(Transaction).where(Transaction.completion_id == comp.id))
                res.paid += 1
        elif kind == "inc" and obj_id:
            inc = db.get(RecurringIncome, obj_id)
            first, last = budget.month_bounds(ln.booked_on.year, ln.booked_on.month)
            confirmed = (
                db.scalar(
                    select(Transaction).where(
                        Transaction.income_id == obj_id, Transaction.booked_on.between(first, last)
                    )
                )
                if inc is not None
                else None
            )
            if confirmed is not None:  # already confirmed (by hand or another import) – link, never overwrite
                tx = confirmed
                res.linked += 1
            elif inc is not None:
                tx = budget.confirm_income(
                    db,
                    inc,
                    ln.booked_on.year,
                    ln.booked_on.month,
                    amount_gr=abs(ln.amount_gr),
                    today=today,
                    user_id=user.id,
                )
                tx.booked_on = ln.booked_on
                res.incomes += 1
        elif kind == "cat" and obj_id:
            cat = db.get(BudgetCategory, obj_id)
            if cat is not None:
                tx = Transaction(
                    booked_on=ln.booked_on,
                    amount_gr=abs(ln.amount_gr) if cat.is_income else -abs(ln.amount_gr),
                    budget_category_id=cat.id,
                    note=ln.description[:300],
                    created_by_id=user.id,
                )
                db.add(tx)
                res.booked += 1
                if wants_rule and learn(db, ln.description, cat.id):
                    res.rules += 1
        if tx is None:
            res.skipped += 1
        db.flush()
        db.add(
            BankLine(
                ref=ln.ref,
                booked_on=ln.booked_on,
                amount_gr=ln.amount_gr,
                description=ln.description[:300],
                transaction_id=tx.id if tx is not None else None,
            )
        )
        db.flush()
    db.commit()
    return res
