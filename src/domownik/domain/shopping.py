"""Shopping list helpers: parsing '2 mleko', guessing the store section."""

from __future__ import annotations

import re

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
    """'2 mleko' → ('mleko', '2'); 'masło x2' → ('masło', '×2'); 'jajka 10 szt' → ('jajka', '10 szt')."""
    text = " ".join((text or "").split())
    if not text:
        return "", None
    m = _LEAD_QTY.match(text)
    if m and not m.group(2)[0].isdigit():
        return _clean_name(m.group(2)), _clean_qty(m.group(1))
    m = _TRAIL_QTY.match(text)
    if m:
        return _clean_name(m.group(1)), _clean_qty(m.group(2))
    return _clean_name(text), None


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
