"""Personal documents of a household member: when they expire and how long the new one will be valid.

Polish rules (2026): ID card 10 years (a child under 5: 5 years), passport 10 years (under 13: 5 years),
driving licence 15 years (categories C/D: 5). The reminder comes early enough for the office's waiting time.
Only the type and the expiry date are kept – never the document's number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from dateutil.relativedelta import relativedelta


@dataclass(frozen=True)
class DocType:
    key: str
    label: str
    icon: str
    years: int | None  # validity of the new one (adult); None = varies, ask again after the renewal
    child_years: int | None = None
    child_below: int | None = None  # age (years) below which child_years applies
    lead_days: int = 60
    how: str = ""

    @property
    def title(self) -> str:
        return f"{self.label} – ważność"

    def validity_years(self, birthday: date | None, renewed_on: date) -> int | None:
        if self.years is None:
            return None
        if birthday and self.child_below and relativedelta(renewed_on, birthday).years < self.child_below:
            return self.child_years
        return self.years


TYPES: tuple[DocType, ...] = (
    DocType(
        "id_card",
        "Dowód osobisty",
        "🪪",
        10,
        child_years=5,
        child_below=5,
        lead_days=60,
        how="Wniosek w dowolnym urzędzie gminy albo przez gov.pl (z profilem zaufanym); wydanie do 30 dni.",
    ),
    DocType(
        "passport",
        "Paszport",
        "🛂",
        10,
        child_years=5,
        child_below=13,
        lead_days=90,
        how="Wniosek osobiście w biurze paszportowym (urząd wojewódzki), dziecko z obojgiem rodziców albo "
        "ze zgodą drugiego; wydanie do 30 dni, przed wakacjami bywa dłużej.",
    ),
    DocType(
        "driving",
        "Prawo jazdy",
        "🚗",
        15,
        lead_days=60,
        how="Wniosek w wydziale komunikacji starostwa albo przez gov.pl; ok. 2–4 tygodni. "
        "Kategorie C/D ważne 5 lat (z badaniami).",
    ),
    DocType(
        "ehic",
        "EKUZ",
        "💳",
        None,
        lead_days=30,
        how="Wniosek w NFZ albo przez Internetowe Konto Pacjenta; zwykle od ręki do kilku dni.",
    ),
)
BY_KEY = {d.key: d for d in TYPES}


def recurrence_for(doc: DocType, birthday: date | None, expires: date) -> dict:
    """After "Zrobione" (renewed) the next expiry comes N years later; EHIC asks for the new date."""
    years = doc.validity_years(birthday, expires)
    if years is None:
        return {"mode": "once", "date": expires.isoformat()}
    return {"mode": "interval", "every": years, "unit": "year", "anchor": "completion"}
