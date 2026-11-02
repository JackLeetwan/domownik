"""Recurrence engine – pure functions, no database access.

A rule is a plain dict stored as JSON on a task:

    {"mode": "interval", "every": 3, "unit": "month", "anchor": "completion", "km": 15000}
    {"mode": "monthly", "day": 10}
    {"mode": "fixed", "dates": ["03-15", "09-15"]}
    {"mode": "easter", "offset": 60}
    {"mode": "nth_weekday", "month": 5, "weekday": "sun", "n": 1}
    {"mode": "once"}

``anchor`` (interval only):
  * ``completion`` – the next due date is counted from the day the task was done,
  * ``due`` – from the previous due date, regardless of when it was done.

Calendar modes (monthly, fixed, easter, nth_weekday) always advance to the next
calendar occurrence after the previous due date, so a late payment does not
shift the schedule and every period has to be ticked off.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

from dateutil.easter import easter
from dateutil.relativedelta import FR, MO, SA, SU, TH, TU, WE, relativedelta

Rule = dict[str, Any]

MODES = ("interval", "monthly", "fixed", "easter", "nth_weekday", "once")
UNITS = ("day", "week", "month", "year")
WEEKDAYS = {"mon": MO, "tue": TU, "wed": WE, "thu": TH, "fri": FR, "sat": SA, "sun": SU}

UNIT_LABELS = {
    "day": ("dzień", "dni", "dni"),
    "week": ("tydzień", "tygodnie", "tygodni"),
    "month": ("miesiąc", "miesiące", "miesięcy"),
    "year": ("rok", "lata", "lat"),
}
WEEKDAY_LABELS = {
    "mon": "poniedziałek",
    "tue": "wtorek",
    "wed": "środa",
    "thu": "czwartek",
    "fri": "piątek",
    "sat": "sobota",
    "sun": "niedziela",
}
MONTH_GENITIVE = [
    "",
    "stycznia",
    "lutego",
    "marca",
    "kwietnia",
    "maja",
    "czerwca",
    "lipca",
    "sierpnia",
    "września",
    "października",
    "listopada",
    "grudnia",
]

MAX_ITER = 10_000


class RuleError(ValueError):
    pass


def validate(rule: Rule) -> Rule:
    """Return a normalised copy of ``rule`` or raise RuleError."""
    mode = rule.get("mode")
    if mode not in MODES:
        raise RuleError(f"Nieznany tryb powtarzania: {mode!r}")
    if mode == "interval":
        every = int(rule.get("every") or 0)
        unit = rule.get("unit")
        if every < 1:
            raise RuleError("Interwał musi być ≥ 1")
        if unit not in UNITS:
            raise RuleError(f"Nieznana jednostka: {unit!r}")
        anchor = rule.get("anchor") or "completion"
        if anchor not in ("completion", "due"):
            raise RuleError(f"Nieznana kotwica: {anchor!r}")
        out: Rule = {"mode": mode, "every": every, "unit": unit, "anchor": anchor}
        if rule.get("km"):
            km = int(rule["km"])
            if km < 1:
                raise RuleError("Próg km musi być dodatni")
            out["km"] = km
        return out
    if mode == "monthly":
        day = int(rule.get("day") or 0)
        if not 1 <= day <= 31:
            raise RuleError("Dzień miesiąca musi być w zakresie 1–31")
        return {"mode": mode, "day": day}
    if mode == "fixed":
        dates = rule.get("dates") or []
        if isinstance(dates, str):
            dates = [d.strip() for d in dates.replace(";", ",").split(",") if d.strip()]
        norm = sorted({_parse_mmdd(d) for d in dates})
        if not norm:
            raise RuleError("Podaj co najmniej jedną datę (MM-DD)")
        return {"mode": mode, "dates": [f"{m:02d}-{d:02d}" for m, d in norm]}
    if mode == "easter":
        return {"mode": mode, "offset": int(rule.get("offset") or 0)}
    if mode == "nth_weekday":
        month = int(rule.get("month") or 0)
        n = int(rule.get("n") or 0)
        weekday = rule.get("weekday")
        if not 1 <= month <= 12:
            raise RuleError("Miesiąc musi być w zakresie 1–12")
        if n not in (1, 2, 3, 4, 5, -1):
            raise RuleError("n musi być 1–5 albo -1 (ostatni)")
        if weekday not in WEEKDAYS:
            raise RuleError(f"Nieznany dzień tygodnia: {weekday!r}")
        return {"mode": mode, "month": month, "weekday": weekday, "n": n}
    return {"mode": "once"}


def _parse_mmdd(value: str) -> tuple[int, int]:
    try:
        m, d = (int(p) for p in str(value).strip().split("-"))
    except ValueError as exc:
        raise RuleError(f"Zły format daty {value!r}, oczekiwano MM-DD") from exc
    if not 1 <= m <= 12 or not 1 <= d <= 31 or d > _days_in_month(2024, m):
        raise RuleError(f"Nieprawidłowa data {value!r}")
    return m, d


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _clamp(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, _days_in_month(year, month)))


def _step(rule: Rule, k: int = 1) -> relativedelta:
    n = rule["every"] * k
    return relativedelta(**{f"{rule['unit']}s": n})


def _calendar_dates_in_year(rule: Rule, year: int) -> list[date]:
    mode = rule["mode"]
    if mode == "monthly":
        return [_clamp(year, m, rule["day"]) for m in range(1, 13)]
    if mode == "fixed":
        return [_clamp(year, *_parse_mmdd(d)) for d in rule["dates"]]
    if mode == "easter":
        return [easter(year) + timedelta(days=rule["offset"])]
    if mode == "nth_weekday":
        wd = WEEKDAYS[rule["weekday"]](rule["n"])
        if rule["n"] > 0:
            d = date(year, rule["month"], 1) + relativedelta(weekday=wd)
        else:
            d = _clamp(year, rule["month"], 31) + relativedelta(weekday=wd)
        return [d] if d.month == rule["month"] else []
    raise RuleError(f"{mode} nie jest trybem kalendarzowym")


def is_calendar(rule: Rule) -> bool:
    return rule["mode"] in ("monthly", "fixed", "easter", "nth_weekday")


def calendar_on_or_after(rule: Rule, start: date) -> date:
    """First calendar occurrence on or after ``start``."""
    for year in range(start.year, start.year + 10):
        for d in sorted(_calendar_dates_in_year(rule, year)):
            if d >= start:
                return d
    raise RuleError("Brak wystąpienia w ciągu 10 lat")


def first_due(rule: Rule, today: date, *, last_done: date | None = None) -> date | None:
    """Suggested first due date for a newly created task."""
    rule = validate(rule)
    if rule["mode"] == "once":
        return None
    if is_calendar(rule):
        return calendar_on_or_after(rule, today)
    base = last_done or today
    return base + _step(rule)


def next_due(rule: Rule, *, previous_due: date | None, done_on: date) -> date | None:
    """Next due date after the task was completed on ``done_on``.

    Returns None when the task does not repeat.
    """
    rule = validate(rule)
    mode = rule["mode"]
    if mode == "once":
        return None
    if is_calendar(rule):
        base = previous_due or done_on
        return calendar_on_or_after(rule, base + timedelta(days=1))
    if rule["anchor"] == "due" and previous_due is not None:
        nxt = previous_due + _step(rule)
        # Long overdue: skip periods that are already in the past relative to done_on
        k = 1
        while nxt <= done_on - _period_len(rule) and k < MAX_ITER:
            k += 1
            nxt = previous_due + _step(rule, k)
        return nxt
    return done_on + _step(rule)


def _period_len(rule: Rule) -> timedelta:
    approx = {"day": 1, "week": 7, "month": 28, "year": 365}[rule["unit"]]
    return timedelta(days=approx * rule["every"])


def occurrences(rule: Rule, first: date | None, start: date, end: date) -> list[date]:
    """All due dates within [start, end], beginning with the series' next due date ``first``.

    Occurrences before ``first`` are never produced (they are either done or
    represented by ``first`` itself when overdue). For completion-anchored
    intervals the projection assumes each occurrence is done on time.
    """
    if first is None or end < start:
        return []
    rule = validate(rule)
    if rule["mode"] == "once":
        return [first] if start <= first <= end else []
    return [d for d in _iter_from(rule, first, end) if d >= start]


def _iter_from(rule: Rule, first: date, end: date) -> Iterator[date]:
    if is_calendar(rule):
        d = first
        for _ in range(MAX_ITER):
            if d > end:
                return
            yield d
            d = calendar_on_or_after(rule, d + timedelta(days=1))
        return
    for k in range(MAX_ITER):
        d = first + _step(rule, k)  # multiply from first – no month-end drift
        if d > end:
            return
        yield d


def describe(rule: Rule) -> str:
    """Human readable Polish description of a rule."""
    try:
        rule = validate(rule)
    except RuleError:
        return "?"
    mode = rule["mode"]
    if mode == "once":
        return "jednorazowo"
    if mode == "interval":
        every, unit = rule["every"], rule["unit"]
        text = f"co {_plural(every, unit)}" if every > 1 else f"co {UNIT_LABELS[unit][0]}"
        if rule.get("km"):
            text += f" lub {rule['km']:,} km".replace(",", " ")
        if rule["anchor"] == "due":
            text += " (od terminu)"
        return text
    if mode == "monthly":
        return f"co miesiąc, {rule['day']}. dnia"
    if mode == "fixed":
        parts = []
        for d in rule["dates"]:
            m, day = _parse_mmdd(d)
            parts.append(f"{day} {MONTH_GENITIVE[m]}")
        return "co roku: " + ", ".join(parts)
    if mode == "easter":
        off = rule["offset"]
        if off == 0:
            return "Wielkanoc (co roku)"
        return f"Wielkanoc {'+' if off > 0 else '−'}{abs(off)} dni (co roku)"
    if mode == "nth_weekday":
        n = "ostatnia/i" if rule["n"] == -1 else f"{rule['n']}."
        return f"{n} {WEEKDAY_LABELS[rule['weekday']]} {MONTH_GENITIVE[rule['month']]}"
    return "?"


def _plural(n: int, unit: str) -> str:
    one, few, many = UNIT_LABELS[unit]
    if n == 1:
        form = one
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        form = few
    else:
        form = many
    return f"{n} {form}"
