"""Shopping list helpers: parsing '2 mleko', guessing the store section."""

from __future__ import annotations

import re

from domownik.domain.grocery_names import UNIT_FORMS, UNIT_WORDS, canonical, split_unit, unit_word

# section key → label; order = walking order in a typical Polish supermarket
SECTIONS: dict[str, str] = {
    "warzywa": "🥕 Warzywa i owoce",
    "pieczywo": "🥖 Pieczywo",
    "nabial": "🧀 Nabiał",
    "mieso": "🥩 Mięso i ryby",
    "suche": "🍝 Sypkie, przetwory",
    "napoje": "🧃 Napoje",
    "slodycze": "🍫 Słodycze i przekąski",
    "mrozonki": "🧊 Mrożonki",
    "chemia": "🧴 Chemia i higiena",
    "dziecko": "🧸 Dla dzieci",
    "apteka": "💊 Apteka",
    "budowlane": "🔨 Budowlane i auto",
    "inne": "📦 Inne",
}

KEYWORDS: dict[str, tuple[str, ...]] = {
    "warzywa": (
        "ziemniak",
        "pomidor",
        "ogór",
        "marchew",
        "cebul",
        "czosn",
        "sałat",
        "papryk",
        "jabł",
        "banan",
        "cytryn",
        "pomarańcz",
        "mandaryn",
        "gruszk",
        "winogron",
        "truskaw",
        "borówk",
        "brokuł",
        "kalafior",
        "kapust",
        "pietrusz",
        "szczypior",
        "koper",
        "awokado",
        "kiwi",
        "pieczark",
        "rzodkiew",
        "seler",
        "por|pory$",
    ),
    "pieczywo": ("chleb", "bułk", "bagiet", "rogal", "chałk", "tortill", "pieczyw"),
    "nabial": (
        "mleko",
        "ser|sery|serek|serki$",
        "jogurt",
        "masło",
        "śmietan",
        "twaróg",
        "kefir",
        "maślank",
        "jaj",
        "żółtk",
        "ciasto francuskie",
        "ciasto filo",
        "serek",
        "mozzarel",
        "parmezan",
        "margaryn",
    ),
    "mieso": (
        "kurczak",
        "pierś",
        "mięso",
        "mielon",
        "schab",
        "szynk",
        "kiełbas",
        "parówk",
        "boczek",
        "wędlin",
        "ryba",
        "łosoś",
        "dorsz",
        "tuńczyk",
        "indyk",
        "wołow",
        "wieprz",
        "salami",
    ),
    "suche": (
        "makaron",
        "ryż",
        "kasz",
        "mąk",
        "cukier",
        "sól",
        "olej",
        "oliw",
        "ocet",
        "płatki",
        "musli",
        "konserw",
        "fasol",
        "groch",
        "przypraw",
        "pieprz",
        "cynamon",
        "kurkum",
        "gałka muszkat",
        "ketchup",
        "majonez",
        "musztard",
        "dżem",
        "miód",
        "kawa",
        "herbat",
        "kakao",
        "sos",
        "passat",
        "koncentrat",
    ),
    "napoje": ("woda", "sok", "napój", "piwo", "wino", "cola", "lemoniad"),
    "slodycze": ("czekolad", "ciastk", "chips", "baton", "lody", "żelk", "cukierk", "orzesz", "paluszk", "wafl"),
    "mrozonki": ("mrożon", "pizza", "frytki", "pierogi"),
    "chemia": (
        "papier toalet",
        "ręcznik papier",
        "płyn",
        "proszek",
        "kapsułk",
        "tabletki do zmyw",
        "gąbk",
        "worki",
        "szampon",
        "mydło",
        "pasta do zęb",
        "szczoteczk",
        "dezodorant",
        "żel pod prysz",
        "chusteczk",
        "folia",
        "domestos",
        "zmywak",
        "sól do zmyw",
        "odplamiacz",
        "płatki kosmet",
    ),
    "dziecko": ("pielusz", "pampers", "chusteczki naw", "kaszka", "zabawk"),
    "apteka": ("apap", "ibuprom", "paracetamol", "witamin", "plaster", "syrop", "lek", "termometr", "nurofen"),
    "budowlane": (
        "śrub",
        "wkręt",
        "farb",
        "pędzel",
        "papier ścier",
        "taśm",
        "klej",
        "silikon",
        "żarów",
        "bateri",
        "olej silnik",
        "płyn do spryskiw",
        "wycieraczk",
        "pasta polersk",
        "lakier",
        "kołk",
        "wiertł",
    ),
}

_LEAD_QTY = re.compile(
    r"^(\d+(?:[.,]\d+)?\s*(?:x|×|szt\.?|kg|dag|g|l|ml|op\.?|opak\.?|paczk[ia]|but\.?|puszk[ia])?)\s+(.+)$",
    re.IGNORECASE,
)
_TRAIL_QTY = re.compile(
    r"^(.+?)\s+((?:x|×)\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*(?:x|×|szt\.?|kg|dag|g|l|ml|op\.?|opak\.?))$",
    re.IGNORECASE,
)


def parse_entry(text: str) -> tuple[str, str | None]:
    """'2 mleko' → ('Mleko', '2'); 'masło x2' → ('Masło', '×2'); 'jajka 10 szt' → ('Jajka', '10 szt');
    recipe lines in the list's own form: '5 jajek' → ('Jajka', '5'), 'laska cynamonu' → ('Cynamon', '1 laska')."""
    name, qty = _split_qty(text)
    if not name:
        return "", None
    name, qty = split_unit(name, qty)
    return _clean_name(canonical(name)), qty


def _split_qty(text: str) -> tuple[str, str | None]:
    text = " ".join((text or "").split())
    if not text:
        return "", None
    m = _LEAD_QTY.match(text)
    if m and not m.group(2)[0].isdigit():
        return m.group(2), _clean_qty(m.group(1))
    m = _TRAIL_QTY.match(text)
    if m:
        return m.group(1), _clean_qty(m.group(2))
    return text, None


# quantity → (number, kind, factor to the base unit): mass in g, volume in ml, pieces
_UNITS = {
    "g": ("g", 1),
    "dag": ("g", 10),
    "kg": ("g", 1000),
    "ml": ("ml", 1),
    "l": ("ml", 1000),
    "": ("szt", 1),
    "szt": ("szt", 1),
    "szt.": ("szt", 1),
}
_QTY = re.compile(r"^(×)?\s*(\d+(?:[.,]\d+)?)\s*([a-ząćęłńóśźż.]*)$", re.IGNORECASE)


def _num(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def _read_qty(qty: str):
    m = _QTY.match(qty.strip())
    if not m:
        return None
    times, num, unit = m.group(1), float(m.group(2).replace(",", ".")), m.group(3).lower()
    if unit in _UNITS:
        kind, factor = _UNITS[unit]
    else:  # kitchen units in any form: 'łyżki' and 'łyżek' are one; others add up only with the same word
        kind, factor = UNIT_FORMS.get(unit.rstrip("."), unit), 1
    return num * factor, kind, unit, bool(times)


def add_qty(a: str | None, b: str | None) -> str | None:
    """Two quantities of the same thing together: '250 g' + '250 g' → '500 g', '1 kg' + '500 g' → '1,5 kg',
    '2' + '3' → '5', '×2' + '×1' → '×3'. Units that do not add up stay side by side: '250 g + 2'."""
    if not a or not b:
        return a or b
    qa, qb = _read_qty(a), _read_qty(b)
    if not qa or not qb or qa[1] != qb[1]:
        return f"{a} + {b}"[:30]
    total, kind = qa[0] + qb[0], qa[1]
    if kind == "g":
        return f"{_num(total / 1000)} kg" if "kg" in (qa[2], qb[2]) and total >= 1000 else f"{_num(total)} g"
    if kind == "ml":
        return f"{_num(total / 1000)} l" if "l" in (qa[2], qb[2]) and total >= 1000 else f"{_num(total)} ml"
    if kind == "szt":
        if qa[3] and qb[3]:
            return f"×{_num(total)}"
        return f"{_num(total)} szt" if "szt" in qa[2] + qb[2] else _num(total)
    if kind in UNIT_WORDS:
        return f"{_num(total)} {unit_word(kind, total)}"
    return f"{_num(total)} {qa[2]}"


def _clean_name(name: str) -> str:
    name = name.strip(" ,;-")
    return name[:1].upper() + name[1:] if name else name


def _clean_qty(qty: str) -> str:
    qty = qty.strip().replace("x", "×").replace("X", "×")
    qty = re.sub(r"^(\d+(?:[.,]\d+)?)\s*×$", r"×\1", qty)  # '2x' → '×2'
    return qty.replace("× ", "×")


def guess_section(name: str) -> str:
    low = name.lower()
    # longer keywords first, so 'papier ścierny' beats 'papier', 'płatki kosmetyczne' beats 'płatki'
    pairs = sorted(((kw, sec) for sec, kws in KEYWORDS.items() for kw in kws), key=lambda p: -len(p[0]))
    for kw, sec in pairs:
        # 'ser|sery$' = whole words only (so 'serwetki' is not cheese); otherwise a word prefix
        pattern = rf"(^|\W)({kw[:-1]})(\W|$)" if kw.endswith("$") else r"(^|\W)" + re.escape(kw)
        if re.search(pattern, low):
            return sec
    return "inne"
