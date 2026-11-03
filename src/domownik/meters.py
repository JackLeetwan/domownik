"""Meter readings → usage: since the last reading, daily / monthly averages, usage per month, cost.

Works on plain (date, value, reset) readings, so the car odometer (Measurement kind="odometer")
is shown with the same numbers and chart – read-only, the mileage forecasts of tasks stay as they were.
"""

from __future__ import annotations

import calendar
import itertools
from dataclasses import dataclass, field
from datetime import date, timedelta

from markupsafe import Markup, escape

from domownik.domain.money import format_pln

DAYS_PER_MONTH = 30.44
MONTH_SHORT = ("", "sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru")


@dataclass
class Reading:
    on: date
    value: float
    reset: bool = False
    id: int | None = None


@dataclass
class Segment:
    """Usage between two readings of one meter (start exclusive, end inclusive)."""

    start: date
    end: date
    used: float

    @property
    def days(self) -> int:
        return (self.end - self.start).days


@dataclass
class MonthUsage:
    first: date
    used: float | None  # None = no readings around this month

    @property
    def label(self) -> str:
        return MONTH_SHORT[self.first.month]


@dataclass
class Stats:
    unit: str
    last: Reading | None = None
    since_prev: float | None = None  # usage from the reading before the last one
    since_days: int | None = None
    daily: float | None = None
    months: list[MonthUsage] = field(default_factory=list)
    unit_price: float | None = None

    @property
    def monthly(self) -> float | None:
        return None if self.daily is None else self.daily * DAYS_PER_MONTH

    def cost(self, used: float | None) -> int | None:
        """Grosze for that much usage, when the meter has a price."""
        if used is None or not self.unit_price:
            return None
        return round(used * self.unit_price * 100)

    @property
    def has_chart(self) -> bool:
        return sum(1 for m in self.months if m.used) >= 1


def ordered(readings: list[Reading]) -> list[Reading]:
    return sorted(readings, key=lambda r: (r.on, r.id or 0))


def segments(readings: list[Reading]) -> list[Segment]:
    out = []
    rs = ordered(readings)
    for prev, cur in itertools.pairwise(rs):
        if cur.reset or cur.value < prev.value:
            continue  # a new meter starts over – what was used meanwhile is unknown
        out.append(Segment(prev.on, cur.on, cur.value - prev.value))
    return out


def _month_first(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def per_month(segs: list[Segment], today: date, months: int = 12) -> list[MonthUsage]:
    """Usage of the last `months` calendar months (this one included), each segment spread evenly over its days."""
    first = _add_months(_month_first(today), -(months - 1))
    totals: dict[date, float] = {}
    for s in segs:
        if s.days <= 0:  # two readings the same day
            key = _month_first(s.end)
            totals[key] = totals.get(key, 0.0) + s.used
            continue
        rate = s.used / s.days
        d = s.start + timedelta(days=1)
        while d <= s.end:
            key = _month_first(d)
            month_end = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
            chunk_end = min(month_end, s.end)
            n = (chunk_end - d).days + 1
            totals[key] = totals.get(key, 0.0) + rate * n
            d = chunk_end + timedelta(days=1)
    return [MonthUsage(_add_months(first, k), totals.get(_add_months(first, k))) for k in range(months)]


def compute(readings: list[Reading], *, unit: str, today: date, unit_price: float | None = None) -> Stats:
    rs = ordered(readings)
    st = Stats(unit=unit, unit_price=unit_price)
    if not rs:
        return st
    st.last = rs[-1]
    if len(rs) >= 2 and not rs[-1].reset and rs[-1].value >= rs[-2].value:
        st.since_prev = rs[-1].value - rs[-2].value
        st.since_days = (rs[-1].on - rs[-2].on).days
    segs = segments(rs)
    days = sum(s.days for s in segs if s.days > 0)
    if days:
        st.daily = sum(s.used for s in segs if s.days > 0) / days
    st.months = per_month(segs, today)
    return st


def neighbours(readings: list[Reading], on: date) -> tuple[Reading | None, Reading | None]:
    """The reading before (or on) `on` and the first one after it."""
    rs = ordered(readings)
    before = [r for r in rs if r.on <= on]
    after = [r for r in rs if r.on > on]
    return (before[-1] if before else None), (after[0] if after else None)


def check_new(readings: list[Reading], on: date, value: float, reset: bool) -> str | None:
    """A warning when the new reading goes backwards (unless it is a new / zeroed meter)."""
    before, after = neighbours(readings, on)
    if not reset and before is not None and value < before.value:
        return (
            f"Odczyt {fmt_num(value)} jest mniejszy niż poprzedni ({fmt_num(before.value)}, {fmt_day(before.on)}). "
            "Jeśli licznik wymieniono albo wyzerowano, zaznacz „Nowy licznik / reset”."
        )
    if after is not None and not after.reset and value > after.value:
        return (
            f"Odczyt {fmt_num(value)} jest większy niż późniejszy ({fmt_num(after.value)}, {fmt_day(after.on)}) – "
            "sprawdź datę i wartość."
        )
    return None


# ---------------------------------------------------------------- formatting


def fmt_num(v: float | None, decimals: int = 3) -> str:
    """12345.6 → '12 345,6' (non-breaking space, trailing zeros dropped)."""
    if v is None:
        return "—"
    text = f"{v:,.{decimals}f}".replace(",", "\u00a0")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def fmt_day(d: date) -> str:
    return f"{d.day}.{d.month:02d}.{d.year}"


def parse_number(raw) -> float | None:
    text = str(raw or "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError("Wpisz liczbę, np. 1234,5") from exc
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        raise ValueError("Wpisz liczbę nie mniejszą od zera")
    return value


def bar_chart(st: Stats, *, height: int = 120) -> Markup:
    """Usage per month as an inline SVG (no script); colours come from CSS (.meter-chart)."""
    months = st.months
    if not months:
        return Markup("")
    peak = max((m.used or 0) for m in months) or 1
    bar_w, gap, top, bottom = 22, 8, 16, 18
    width = len(months) * (bar_w + gap) - gap
    plot = height - top - bottom
    parts = [
        f'<svg class="meter-chart" viewBox="0 0 {width} {height}" role="img" preserveAspectRatio="xMidYMid meet" '
        f'aria-label="Zużycie w miesiącach ({escape(st.unit)})">'
    ]
    for i, m in enumerate(months):
        x = i * (bar_w + gap)
        label = f"{m.label} {m.first.year}"
        if m.used is None:
            title = f"{label}: brak odczytów"
            parts.append(
                f'<rect class="mc-empty" x="{x}" y="{top + plot - 2}" width="{bar_w}" height="2"><title>{title}</title></rect>'
            )
        else:
            h = max(1.0, plot * m.used / peak)
            cost = st.cost(m.used)
            title = f"{label}: {fmt_num(m.used, 1)} {st.unit}" + (f" ≈ {format_pln(cost)}" if cost else "")
            parts.append(
                f'<rect class="mc-bar" x="{x}" y="{top + plot - h:.1f}" width="{bar_w}" height="{h:.1f}" rx="3">'
                f"<title>{escape(title)}</title></rect>"
            )
            if m.used and h > 0:
                parts.append(
                    f'<text class="mc-val" x="{x + bar_w / 2}" y="{top + plot - h - 3:.1f}" text-anchor="middle">'
                    f"{escape(_short(m.used))}</text>"
                )
        parts.append(f'<text class="mc-lbl" x="{x + bar_w / 2}" y="{height - 4}" text-anchor="middle">{m.label}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def _short(v: float) -> str:
    if v >= 10000:
        return f"{v / 1000:.0f}k"
    if v >= 1000:
        return f"{v / 1000:.1f}k".replace(".", ",")
    if v >= 10:
        return f"{v:.0f}"
    return fmt_num(v, 1)
