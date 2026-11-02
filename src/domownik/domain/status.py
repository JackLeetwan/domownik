"""Task status: overdue / due today / within the reminder window / later."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

STATUS_LABELS = {
    "overdue": "zaległe",
    "today": "dziś",
    "soon": "wkrótce",
    "later": "później",
    "none": "bez terminu",
}

BUCKETS = (
    ("overdue", "Zaległe"),
    ("week", "Najbliższe 7 dni"),
    ("month", "Do 30 dni"),
    ("later", "Później"),
)


@dataclass(frozen=True)
class Due:
    date: date | None  # effective due date (earliest of calendar date and km forecast)
    by_km: bool  # True when the km forecast is earlier than the calendar date
    status: str
    days_left: int | None

    @property
    def bucket(self) -> str:
        if self.days_left is None:
            return "later"
        if self.days_left < 0:
            return "overdue"
        if self.days_left <= 7:
            return "week"
        if self.days_left <= 30:
            return "month"
        return "later"


def forecast_km_date(readings: list[tuple[date, float]], target_km: int, today: date) -> date | None:
    """Date when ``target_km`` will be reached, from the average daily distance of the readings."""
    if not readings:
        return None
    readings = sorted(readings)
    last_day, last_km = readings[-1]
    if last_km >= target_km:
        return min(last_day, today)
    window = [r for r in readings if r[0] >= last_day - timedelta(days=365)]
    first_day, first_km = window[0]
    span = (last_day - first_day).days
    if span < 14 or last_km <= first_km:
        return None
    per_day = (last_km - first_km) / span
    return last_day + timedelta(days=int((target_km - last_km) / per_day))


def compute_due(
    next_due_date: date | None,
    lead_days: int,
    today: date,
    *,
    next_due_km: int | None = None,
    km_readings: list[tuple[date, float]] | None = None,
) -> Due:
    eff = next_due_date
    by_km = False
    if next_due_km and km_readings:
        km_date = forecast_km_date(km_readings, next_due_km, today)
        if km_date and (eff is None or km_date < eff):
            eff, by_km = km_date, True
    if eff is None:
        return Due(None, False, "none", None)
    days = (eff - today).days
    if days < 0:
        status = "overdue"
    elif days == 0:
        status = "today"
    elif days <= max(lead_days, 0):
        status = "soon"
    else:
        status = "later"
    return Due(eff, by_km, status, days)


def humanize_days(days: int | None) -> str:
    if days is None:
        return "bez terminu"
    if days == 0:
        return "dziś"
    if days == 1:
        return "jutro"
    if days == -1:
        return "wczoraj"
    if days < 0:
        return f"{-days} dni temu"
    if days < 60:
        return f"za {days} dni"
    months = round(days / 30.4)
    if months < 24:
        return f"za {months} mies."
    return f"za {days // 365} lat"
