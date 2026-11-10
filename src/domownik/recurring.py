"""🔁 Stałe płatności – found in the imported bank statements, nothing to type in.

The same payee charged again and again at a steady rhythm (about monthly, quarterly or yearly) is a
standing payment: a subscription (Netflix), a bill (the phone, the housing association), an instalment, a
standing transfer. Shops are not: their visits come at no rhythm and the amounts jump.

For each: what it usually costs, how often, when it should come next, whether it got dearer – and, for the
ones that stopped coming, "chyba zakończone" (worth checking it really is cancelled).
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik.models import BankLine

LOOK_BACK_DAYS = 800  # two years and a bit: a yearly payment shows up twice
MIN_CHARGES = 3
MIN_CHARGES_YEARLY = 2  # the same sum a year apart (a policy, a domain) – a statement rarely goes back further
# cadence: (label, typical gap in days, the median gap must fall in [lo, hi], months it stands for)
CADENCES = [
    ("co miesiąc", 30, 24, 38, 1),
    ("co kwartał", 91, 80, 100, 3),
    ("co rok", 365, 340, 390, 12),
]
GAP_SLACK = 10  # a gap counts as "on rhythm" within ± this many days of the median
MIN_ON_RHYTHM = 0.6  # share of the gaps on rhythm
STEADY = 0.05  # amounts within ±5 % → a fixed price ("stała kwota")
DEARER = 0.03  # the last charge above the usual one by more → "drożej"

# what banks add after the payee: "NETFLIX.COM ZAKUP PRZY UŻYCIU KARTY - INTERNET", "PLAY, F/123 PRZELEW …"
_TAIL = re.compile(
    r"\s+(ZAKUP PRZY UŻYCIU KARTY|PRZELEW|BLIK|PŁATNOŚĆ|WYPŁATA|POLECENIE ZAPŁATY|ZLECENIE STAŁE|OBCIĄŻENIE)\b.*$",
    re.IGNORECASE,
)
_SKIP = re.compile(r"WYPŁATA GOTÓWKI|BANKOMAT|ATM|PROWIZJA", re.IGNORECASE)  # cash, fees per withdrawal


def payee(description: str) -> str:
    """'NETFLIX.COM ZAKUP PRZY UŻYCIU KARTY - INTERNET' → 'NETFLIX.COM'; 'PLAY, F/50557903/11/25 …' → 'PLAY'."""
    d = _TAIL.sub("", " ".join((description or "").split()))
    return d.split(",")[0].strip(" -–")[:60]


def _key(name: str) -> str:
    """The same payee whatever the invoice number or the month in the text."""
    k = re.sub(r"\d+", "", name.upper())
    k = re.sub(r"[^\wĄĆĘŁŃÓŚŹŻ ]+", " ", k)
    return " ".join(k.split())[:40]


def _pretty(name: str) -> str:
    """'NETFLIX.COM' → 'Netflix.com', 'HBO MAX' → 'HBO Max', 'NETIA S.A.' → 'Netia S.A.' (short ones stay)."""
    if not name.isupper():
        return name
    return " ".join(w if len(w) <= 3 or (len(w) <= 4 and "." in w) else w.capitalize() for w in name.split())


@dataclass
class Standing:
    name: str
    cadence: str
    months: int  # 1 / 3 / 12 – for the monthly sum
    usual_gr: int  # the price now: the median of the last 3 charges
    last_gr: int
    last_on: date
    next_on: date
    charges: int
    steady: bool  # a fixed price (a subscription, an instalment) – not a bill that varies
    ended: bool  # did not come when it should have, by a margin
    before_gr: int | None = None  # the price before it went up (fixed prices only)

    @property
    def dearer_gr(self) -> int:
        """How much more than before it costs now – only for fixed prices (a bill just varies)."""
        return self.usual_gr - self.before_gr if self.before_gr else 0

    @property
    def monthly_gr(self) -> int:
        return round(self.usual_gr / self.months)


def find(db: Session, today: date) -> list[Standing]:
    lines = db.scalars(
        select(BankLine)
        .where(BankLine.amount_gr < 0, BankLine.booked_on >= today - timedelta(days=LOOK_BACK_DAYS))
        .order_by(BankLine.booked_on)
    )
    groups: dict[str, list[tuple[date, int, str]]] = defaultdict(list)
    for ln in lines:
        if _SKIP.search(ln.description or ""):
            continue
        name = payee(ln.description)
        key = _key(name)
        if key:
            groups[key].append((ln.booked_on, -ln.amount_gr, name))
    out = []
    for xs in groups.values():
        s = _standing(xs, today)
        if s is not None:
            out.append(s)
    return sorted(out, key=lambda s: (s.ended, -s.monthly_gr))


def _standing(xs: list[tuple[date, int, str]], today: date) -> Standing | None:
    # two charges on one day (a correction, a split) count as one visit
    by_day: dict[date, int] = defaultdict(int)
    for d, amount, _ in xs:
        by_day[d] += amount
    days = sorted(by_day)
    if len(days) < MIN_CHARGES_YEARLY:
        return None
    gaps = [(b - a).days for a, b in pairwise(days)]
    med = statistics.median(gaps)
    cad = next((c for c in CADENCES if c[2] <= med <= c[3]), None)
    if cad is None or (cad[4] < 12 and len(days) < MIN_CHARGES):
        return None
    on_rhythm = sum(1 for g in gaps if abs(g - med) <= GAP_SLACK) / len(gaps)
    if on_rhythm < MIN_ON_RHYTHM:
        return None
    amounts = [by_day[d] for d in days]
    now = round(statistics.median(amounts[-3:]))
    steady = all(abs(a - now) <= now * STEADY for a in amounts[-3:])
    if not steady and len(days) < MIN_CHARGES + 1:  # a few different sums: not enough to call it standing
        return None
    before = None
    if steady and len(amounts) >= 5:
        earlier = round(statistics.median(amounts[:-3]))
        if now > earlier * (1 + DEARER):
            before = earlier
    label, gap, _, _, months = cad
    last = days[-1]
    return Standing(
        name=_pretty(xs[-1][2]),
        cadence=label,
        months=months,
        usual_gr=now,
        last_gr=amounts[-1],
        last_on=last,
        next_on=last + timedelta(days=round(med)),
        charges=len(days),
        steady=steady,
        ended=(today - last).days > med + max(GAP_SLACK * 2, gap // 2),
        before_gr=before,
    )
