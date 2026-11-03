"""Polish public holidays (dni ustawowo wolne od pracy) – shown on the calendar grid, nothing else.

Pure functions; Easter-based ones from dateutil (Western Easter, as the Polish calendar uses).
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from dateutil.easter import easter

FIXED: dict[tuple[int, int], str] = {
    (1, 1): "Nowy Rok",
    (1, 6): "Trzech Króli",
    (5, 1): "Święto Pracy",
    (5, 3): "Święto Konstytucji 3 Maja",
    (8, 15): "Wniebowzięcie NMP",
    (11, 1): "Wszystkich Świętych",
    (11, 11): "Święto Niepodległości",
    (12, 25): "Boże Narodzenie",
    (12, 26): "Drugi dzień Świąt",
}
WIGILIA_FROM = 2025  # a public holiday since 2025


@lru_cache(maxsize=32)
def of_year(year: int) -> dict[date, str]:
    out = {date(year, m, d): name for (m, d), name in FIXED.items()}
    if year >= WIGILIA_FROM:
        out[date(year, 12, 24)] = "Wigilia"
    e = easter(year)
    out[e] = "Wielkanoc"
    out[e + timedelta(days=1)] = "Poniedziałek Wielkanocny"
    out[e + timedelta(days=49)] = "Zielone Świątki"
    out[e + timedelta(days=60)] = "Boże Ciało"
    return out


def name(d: date) -> str | None:
    """'Święto Niepodległości' or None."""
    return of_year(d.year).get(d)
