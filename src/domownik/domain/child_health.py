"""Vaccinations (the Polish mandatory programme, PSO) and health check-ups ("bilanse") of a child, by age.

Ages checked against the short calendar of PSO 2026 (GIS communiqué of 31.10.2025, szczepienia.pzh.gov.pl) and the
check-up ages given by pacjent.gov.pl: 2, 4, 5, 9–10, 13–14, 16–17 years and once more before 19. The doctor sets the exact day – the tasks are reminders that
can be moved with "Przełóż". Only the free, mandatory vaccinations and the routine check-ups are listed;
recommended ones (meningococci, chickenpox, flu…) are worth asking the paediatrician about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from dateutil.relativedelta import relativedelta


@dataclass(frozen=True)
class Visit:
    key: str
    title: str
    months: int  # age in months
    what: str
    kind: str = "vaccine"  # vaccine | checkup
    until: int | None = None  # a check-up can be done until this age (months) – offered till then, not as "past"

    def due(self, birthday: date) -> date:
        return birthday + relativedelta(months=self.months)

    def last_day(self, birthday: date) -> date:
        return birthday + relativedelta(months=self.until or self.months)

    @property
    def icon(self) -> str:
        return "💉" if self.kind == "vaccine" else "🩺"


PLAN: tuple[Visit, ...] = (
    Visit(
        "v2m",
        "Szczepienie – 2. miesiąc",
        2,
        "6w1 (błonica, tężec, krztusiec, polio, Hib, WZW B) – 1. dawka; pneumokoki; rotawirusy",
    ),
    Visit("v4m", "Szczepienie – 4. miesiąc", 4, "6w1 – 2. dawka; pneumokoki – 2. dawka; rotawirusy – 2. dawka"),
    Visit("v6m", "Szczepienie – 6. miesiąc", 6, "6w1 – 3. dawka (przy szczepionkach osobnych WZW B w 7. miesiącu)"),
    Visit("b12m", "Bilans 1-latka", 12, "rozwój, wzrost i waga, słuch i wzrok", "checkup", until=24),
    Visit(
        "v13m",
        "Szczepienie – 13.–15. miesiąc",
        13,
        "odra, świnka, różyczka (MMR) – 1. dawka; pneumokoki – dawka przypominająca",
    ),
    Visit("v16m", "Szczepienie – 16.–18. miesiąc", 16, "6w1 / 5w1 – 4. dawka (dawka uzupełniająca)"),
    Visit("b2y", "Bilans 2-latka", 24, "rozwój mowy i ruchu, wzrost i waga", "checkup", until=48),
    Visit("b4y", "Bilans 4-latka", 48, "wzrok, słuch, ciśnienie, postawa", "checkup", until=60),
    Visit("b5y", "Bilans 5-latka", 60, "gotowość szkolna, wzrok, słuch, mowa, postawa", "checkup", until=72),
    Visit("v6y", "Szczepienie – 6 lat", 72, "błonica, tężec, krztusiec (DTaP), polio (IPV), MMR – 2. dawka"),
    Visit("v9y", "Szczepienie HPV", 108, "zalecane, bezpłatne od 9 do 14 lat – 2 dawki w odstępie 6–12 miesięcy"),
    Visit("b9y", "Bilans 9–10-latka", 108, "wzrost i waga, postawa, ciśnienie, wzrok", "checkup", until=132),
    Visit("b13y", "Bilans 13–14-latka", 156, "dojrzewanie, postawa, ciśnienie", "checkup", until=180),
    Visit("v14y", "Szczepienie – 14 lat", 168, "błonica, tężec, krztusiec (Tdap) – dawka przypominająca"),
    Visit("b16y", "Bilans 16–17-latka", 192, "ogólny stan zdrowia, ciśnienie", "checkup", until=216),
    Visit("b18y", "Bilans 18-latka", 216, "przed 19. urodzinami; wybór lekarza dla dorosłych", "checkup", until=228),
    Visit("v19y", "Szczepienie – 19 lat", 228, "błonica, tężec (Td) – dawka przypominająca"),
)
BY_KEY = {v.key: v for v in PLAN}
GRACE_DAYS = 31  # a visit that was due less than a month ago is still offered (maybe not done yet)


@dataclass
class Offer:
    visit: Visit
    due: date
    past: bool  # due long ago – most likely done already
    planned: bool  # a task with this title already exists


def plan_for(birthday: date, today: date, existing_titles: set[str]) -> list[Offer]:
    out = []
    for v in PLAN:
        d = v.due(birthday)
        out.append(Offer(v, d, (today - v.last_day(birthday)).days > GRACE_DAYS, v.title.lower() in existing_titles))
    return out
