"""📏 Wymiary: one person's body measurements and the sizes that fit them in given brands – what an online
clothes / shoe order needs. Lives on the person's 🩺 Zdrowie page.

An entry (BodySize) holds what was measured that day; the current value of each measurement is the newest
entry that has it, so measuring just the feet does not wipe the rest. For the owner the chest and waist from
💪 Sylwetka count too (whichever is newer) and the latest weigh-in goes into the copied text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik.models import BodyCheck, BodySize, BodyWeight, FitSize, Item, User

# key, label, group, how to measure
FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("height", "Wzrost", "Sylwetka", "bez butów, plecami do ściany, wzrok prosto"),
    ("chest", "Klatka / biust", "Sylwetka", "w najszerszym miejscu, pod pachami, taśma poziomo, na wydechu"),
    ("waist", "Talia", "Sylwetka", "w najwęższym miejscu (zwykle nad pępkiem), bez wciągania brzucha"),
    ("hips", "Biodra", "Sylwetka", "w najszerszym miejscu pośladków, stopy razem"),
    ("shoulders", "Szerokość ramion", "Sylwetka", "z tyłu, od kości barkowej do kości barkowej"),
    ("neck", "Obwód szyi", "Sylwetka", "u nasady szyi, z palcem pod taśmą – to rozmiar kołnierzyka koszuli"),
    ("biceps", "Biceps", "Ręce i nogi", "ramię luźno, w najszerszym miejscu między barkiem a łokciem"),
    ("forearm", "Przedramię", "Ręce i nogi", "ręka luźno, w najszerszym miejscu tuż pod łokciem"),
    ("thigh", "Udo", "Ręce i nogi", "na stojąco, w najszerszym miejscu, tuż pod kroczem"),
    ("calf", "Łydka", "Ręce i nogi", "na stojąco, w najszerszym miejscu łydki"),
    ("sleeve", "Długość rękawa", "Długości", "od kości barkowej przez lekko zgięty łokieć do nadgarstka"),
    ("inseam", "Nogawka (wewn.)", "Długości", "po wewnętrznej stronie nogi od krocza do podłogi, bez butów"),
    ("outseam", "Długość nogi (zewn.)", "Długości", "z boku, od talii (tam, gdzie pasek) do podłogi, bez butów"),
    ("head", "Obwód głowy", "Głowa i dłonie", "nad brwiami i uszami, w najszerszym miejscu (czapki, kaski)"),
    ("hand", "Obwód dłoni", "Głowa i dłonie", "dłoń otwarta, nad kostkami, bez kciuka (rękawiczki)"),
    ("wrist", "Nadgarstek", "Głowa i dłonie", "tuż nad kostką nadgarstka (zegarek, bransoletka)"),
    ("foot_len", "Długość stopy", "Stopy", "na stojąco, od pięty do najdłuższego palca, wieczorem, dłuższa stopa"),
    ("foot_width", "Szerokość stopy", "Stopy", "na stojąco, w najszerszym miejscu (u nasady palców)"),
)
# ⓘ the longer guide of each measurement: what it decides and how to take it, step by step
GUIDE: dict[str, tuple[str, tuple[str, ...]]] = {
    "height": ("Rozmiar w tabelach marek (np. „176–182”), długość kurtek i płaszczy, rama roweru.", (
        "Stań bez butów plecami do ściany: pięty, pośladki i łopatki dotykają ściany.",
        "Patrz prosto przed siebie, broda równolegle do podłogi.",
        "Połóż na czubku głowy książkę dociśniętą do ściany i zaznacz ołówkiem jej dolną krawędź.",
        "Zmierz od podłogi do kreski – najlepiej rano, wieczorem jest się o ok. 1 cm niższym.",
    )),
    "chest": ("Koszulki, koszule, bluzy, swetry, kurtki – główny wymiar „góry” (S/M/L, 48/50).", (
        "Stań swobodnie, ręce wzdłuż ciała, na samej bieliźnie albo cienkiej koszulce.",
        "Poprowadź taśmę pod pachami, przez najszerszy punkt klatki (u kobiet – przez biust).",
        "Na plecach taśma musi biec poziomo – najłatwiej sprawdzić w lustrze albo z pomocą drugiej osoby.",
        "Odczytaj na spokojnym wydechu; taśma przylega, ale nie uciska.",
    )),
    "waist": ("Spodnie i spódnice (rozmiar W w jeansach), paski, sukienki.", (
        "Znajdź najwęższe miejsce tułowia – zwykle ok. 2–3 cm nad pępkiem; pochyl się w bok, zgięcie pokazuje talię.",
        "Owiń taśmę poziomo, nie wciągaj brzucha.",
        "Odczytaj na spokojnym wydechu.",
        "Jeansy noszone niżej: zmierz też obwód tam, gdzie leży pasek – zapisz w notatce pomiaru.",
    )),
    "hips": ("Spodnie, spódnice, sukienki, bielizna; przy szerszych biodrach decyduje o rozmiarze spodni.", (
        "Stań ze złączonymi stopami.",
        "Poprowadź taśmę przez najbardziej wystający punkt pośladków.",
        "Sprawdź w lustrze z boku, czy taśma jest poziomo, i odczytaj.",
    )),
    "shoulders": ("Koszule, marynarki, kurtki, płaszcze – za wąskie w ramionach nie da się poprawić u krawca.", (
        "Mierzy druga osoba, Ty stoisz prosto, ręce luźno.",
        "Wymacaj z tyłu kości barkowe – kostne „rogi” na końcu obojczyków, nad ramieniem.",
        "Zmierz w linii prostej po plecach od jednej kości barkowej do drugiej.",
        "Najprościej porównać z koszulą, która dobrze leży: szew od szwu na plecach.",
    )),
    "neck": ("Rozmiar kołnierzyka koszuli (np. 41) – koszule wizytowe sprzedaje się właśnie po nim.", (
        "Owiń taśmę u nasady szyi, tam, gdzie leży kołnierzyk – poniżej jabłka Adama.",
        "Włóż pod taśmę jeden palec, żeby został luz.",
        "Odczytaj i zaokrąglij w górę do pełnego centymetra (połówki – do 0,5).",
    )),
    "biceps": ("Dopasowane koszule i koszulki (slim), marynarki – przy treningu siłowym często to rękaw jest za ciasny.", (
        "Ręka luźno wzdłuż ciała, bez napinania.",
        "Owiń taśmę wokół ramienia w najszerszym miejscu, zwykle w połowie między barkiem a łokciem.",
        "Taśma poziomo, przylega, ale nie uciska. Napięty biceps możesz zapisać w notatce.",
    )),
    "forearm": ("Mankiety i rękawy dopasowanych koszul, rękawy kurtek, opaski sportowe.", (
        "Ręka luźno, dłoń rozluźniona.",
        "Owiń taśmę w najszerszym miejscu przedramienia – kilka centymetrów pod łokciem.",
    )),
    "thigh": ("Jeansy i spodnie slim/skinny, szorty, spodnie kolarskie – czy nogawka nie będzie za ciasna w udzie.", (
        "Stań w lekkim rozkroku, ciężar na obu nogach.",
        "Owiń taśmę wokół uda w najszerszym miejscu, tuż pod kroczem.",
        "Taśma poziomo, bez uciskania.",
    )),
    "calf": ("Kozaki i wysokie buty, skarpety uciskowe, spodnie z wąską nogawką.", (
        "Stań swobodnie, ciężar na obu nogach.",
        "Owiń taśmę wokół łydki w najszerszym miejscu i odczytaj.",
    )),
    "sleeve": ("Koszule (np. „41/88”), kurtki, płaszcze, swetry – czy rękaw nie będzie za krótki.", (
        "Mierzy druga osoba. Stań prosto, ręka lekko zgięta w łokciu, dłoń na biodrze.",
        "Przyłóż taśmę na kości barkowej.",
        "Prowadź ją po zewnętrznej stronie ręki przez łokieć do kości nadgarstka (koniec mankietu).",
        "Rękaw koszuli w sklepach liczy się czasem od karku – wtedy dodaj połowę szerokości ramion.",
    )),
    "inseam": ("Długość nogawki spodni i jeansów (rozmiar L, np. W32/L32).", (
        "Stań bez butów, stopy lekko rozstawione.",
        "Przyłóż taśmę jak najwyżej w kroczu i prowadź ją po wewnętrznej stronie nogi do podłogi.",
        "Prościej: zmierz szew wewnętrzny spodni, które mają dobrą długość.",
    )),
    "outseam": ("Długość spodni w tabelach niektórych marek, spodnie garniturowe, spodnie z wysokim stanem.", (
        "Stań bez butów, prosto.",
        "Przyłóż taśmę z boku, na wysokości talii – tam, gdzie zwykle leży pasek.",
        "Prowadź ją pionowo po zewnętrznej stronie nogi do podłogi.",
    )),
    "head": ("Czapki, kapelusze, kaski rowerowe i narciarskie (rozmiar = obwód, np. 57).", (
        "Owiń taśmę wokół głowy ok. 1 cm nad brwiami i nad uszami.",
        "Z tyłu przez najbardziej wystający punkt potylicy.",
        "Odczytaj bez uciskania; między rozmiarami wybierz większy.",
    )),
    "hand": ("Rękawiczki (rozmiar 8–10 albo S/M/L), rękawiczki rowerowe i na siłownię.", (
        "Zmierz dłoń, którą piszesz.",
        "Otwórz dłoń, palce razem, kciuk odsunięty.",
        "Owiń taśmę wokół dłoni nad kostkami (u nasady palców), bez kciuka.",
    )),
    "wrist": ("Zegarki i paski do nich, bransoletki, mankiety koszul.", (
        "Owiń taśmę tuż nad kostką nadgarstka (od strony łokcia).",
        "Na pasek zegarka dodaj ok. 1–1,5 cm luzu.",
    )),
    "foot_len": ("Rozmiar butów – obok pokazujemy przybliżony rozmiar EU; tabele marek podają długość wkładki.", (
        "Mierz wieczorem – stopa jest wtedy największa.",
        "Postaw stopę na kartce przy ścianie, piętą do ściany, ciężar ciała na tej nodze.",
        "Zaznacz koniec najdłuższego palca (nie zawsze dużego) i zmierz od ściany do kreski.",
        "Zmierz obie stopy i zapisz dłuższą.",
    )),
    "foot_width": ("Buty o szerszej tęgości (np. „wide”, H, G), buty górskie i biegowe.", (
        "Stań na kartce, ciężar ciała na mierzonej nodze.",
        "Zaznacz po obu stronach najszersze miejsce stopy – u nasady palców.",
        "Zmierz odległość między kreskami.",
    )),
}  # fmt: skip
KEYS = tuple(k for k, *_ in FIELDS)
LABELS = {k: lbl for k, lbl, *_ in FIELDS}
FROM_BODY_CHECK = ("chest", "waist")  # what 💪 Sylwetka measures too

WHAT = (
    "Koszulka", "Koszula", "Bluza", "Sweter", "Kurtka", "Płaszcz", "Spodnie", "Jeansy", "Szorty", "Dresy",
    "Sukienka", "Spódnica", "Garnitur", "Bielizna", "Biustonosz", "Skarpety", "Buty", "Buty sportowe",
    "Kapcie", "Kalosze", "Czapka", "Rękawiczki", "Kombinezon", "Piżama",
)  # fmt: skip

CHILD_EVERY_DAYS = 90  # children's feet grow fast – measure every ~3 months
ADULT_OLD_DAYS = 365


def fmt(v: float | None) -> str:
    if v is None:
        return ""
    return (f"{v:.1f}".rstrip("0").rstrip(".")).replace(".", ",")


def parse_cm(raw) -> float | None:
    """'27,5' / '27.5' / '27,5 cm' → 27.5; nonsense and zero → None; mm given by mistake (275) stays as typed."""
    v = str(raw or "").lower().replace("cm", "").strip().replace(",", ".")
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return round(x, 1) if 0 < x < 300 else None


def eu_shoe(foot_len: float | None) -> str | None:
    """Approximate EU (Paris point) size: 1.5 × (foot + ~1.5 cm of room), to the nearest half."""
    if not foot_len or not 8 <= foot_len <= 35:
        return None
    return fmt(round((foot_len + 1.5) * 1.5 * 2) / 2)


@dataclass
class Value:
    key: str
    value: float
    on: date
    source: str = ""  # "" | "Sylwetka"
    delta: float | None = None  # change since the previous measurement of this one
    since: date | None = None

    @property
    def label(self) -> str:
        return LABELS[self.key]


@dataclass
class Sizes:
    item: Item
    values: dict[str, Value]
    entries: list[BodySize]  # newest first
    fits: list[FitSize]
    weight: float | None = None  # the owner's latest weigh-in (only for the owner)
    weight_on: date | None = None

    @property
    def is_child(self) -> bool:
        return self.item.kind == "child"

    @property
    def shoe(self) -> str | None:
        v = self.values.get("foot_len")
        return eu_shoe(v.value) if v else None

    def stale(self, key: str, today: date) -> bool:
        v = self.values.get(key)
        if v is None:
            return False
        return (today - v.on).days > (CHILD_EVERY_DAYS if self.is_child else ADULT_OLD_DAYS)

    def remind(self, today: date) -> str | None:
        """A child's feet (and height) want measuring again – a line for the page and the overview card."""
        if not self.is_child:
            return None
        foot = self.values.get("foot_len")
        if foot is None:
            return "Zmierz stopę – przyda się przy zamawianiu butów."
        days = (today - foot.on).days
        if days > CHILD_EVERY_DAYS:
            return f"Stopa mierzona {days // 30} mies. temu – czas zmierzyć znowu."
        return None

    def copy_text(self) -> str:
        """Plain text to paste into a shop's size finder or a chat."""
        lines = [f"{self.item.name} – wymiary (cm)"]
        body = [f"{v.label.lower()} {fmt(v.value)}" for k in KEYS if (v := self.values.get(k)) and k[:4] != "foot"]
        if self.weight:
            body.append(f"waga {fmt(self.weight)} kg")
        if body:
            lines.append(", ".join(body))
        foot = self.values.get("foot_len")
        if foot:
            width = self.values.get("foot_width")
            line = f"stopa {fmt(foot.value)} cm" + (f" (szerokość {fmt(width.value)})" if width else "")
            if self.shoe:
                line += f" ≈ EU {self.shoe}"
            lines.append(line)
        if self.fits:
            lines.append("Pasuje: " + "; ".join(fit_label(f) for f in self.fits))
        if self.values:
            lines.append(f"Stan na {max(v.on for v in self.values.values()).isoformat()}")
        return "\n".join(lines)


def fit_label(f: FitSize) -> str:
    return f"{f.what}{f' {f.brand}' if f.brand else ''}: {f.size}"


def load(db: Session, item: Item, user: User) -> Sizes:
    entries = list(
        db.scalars(
            select(BodySize)
            .where(BodySize.item_id == item.id)
            .order_by(BodySize.measured_on.desc(), BodySize.id.desc())
        )
    )
    values: dict[str, Value] = {}
    for e in entries:  # newest first: the first one with a value wins, the next one gives the change
        for k in KEYS:
            x = getattr(e, k)
            if x is None:
                continue
            if k not in values:
                values[k] = Value(k, x, e.measured_on)
            elif values[k].delta is None and e.measured_on < values[k].on:
                values[k].delta, values[k].since = round(values[k].value - x, 1), e.measured_on
    weight = weight_on = None
    if item.user_id is not None and item.user_id == user.id:  # 💪 Sylwetka is strictly the owner's
        check = db.scalars(select(BodyCheck).where(BodyCheck.user_id == user.id).order_by(BodyCheck.day.desc())).first()
        if check is not None:
            for k in FROM_BODY_CHECK:
                x = getattr(check, k)
                if x is not None and (k not in values or check.day > values[k].on):
                    values[k] = Value(k, x, check.day, "Sylwetka")
        w = db.scalars(select(BodyWeight).where(BodyWeight.user_id == user.id).order_by(BodyWeight.day.desc())).first()
        if w is not None:
            weight, weight_on = w.kg, w.day
    fits = list(db.scalars(select(FitSize).where(FitSize.item_id == item.id).order_by(FitSize.what, FitSize.id)))
    return Sizes(item, values, entries, fits, weight, weight_on)


def reminders(db: Session, people: list[Item], user: User, today: date) -> dict[int, str]:
    """Children whose feet want measuring – for the 🩺 Zdrowie overview cards."""
    out = {}
    for p in people:
        if p.kind == "child" and (msg := load(db, p, user).remind(today)):
            out[p.id] = msg
    return out
