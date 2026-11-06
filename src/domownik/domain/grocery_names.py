"""One name per product on the shopping list: recipes say "200 g mąki pszennej", "5 jajek", "laska cynamonu";
the list wants "Mąka pszenna · 200 g", "Jajka · 5", "Cynamon · 1 laska" – so the same thing adds up.

Not a full Polish grammar: a lexicon of common groceries (every form → one canonical name), adjectives
next to such a noun put in agreement with it, and kitchen units pulled out of the name. Unknown words stay
as they were. Only the head of the name changes – text after a comma, a bracket or a preposition
("skórka z cytryny", "wino do gotowania") is kept as written.
"""

from __future__ import annotations

import re

# canonical name, its gender (m/f/n, pl = plural noun) and other forms. Plural canonical only where the
# shop speaks so ("jajka", "drożdże"); "4 pomidory" → "Pomidor · 4" is fine.
_LEXICON = """
f mąka: mąki mąkę mąką
f śmietana: śmietany śmietanę śmietaną
f śmietanka: śmietanki śmietankę śmietanką
f sól: soli solą
f cebula: cebuli cebulę cebulą cebule cebul
f marchewka: marchewki marchewkę marchewek
f marchew: marchwi
f papryka: papryki paprykę papryką papryk
f papryczka: papryczki papryczkę papryczek
f cytryna: cytryny cytrynę cytryn
f limonka: limonki limonkę limonek
f pomarańcza: pomarańczy pomarańczę pomarańcze
f oliwa: oliwy oliwę oliwą
f kurkuma: kurkumy kurkumę
f bazylia: bazylii bazylię
f natka: natki natkę
f pietruszka: pietruszki pietruszkę
f szynka: szynki szynkę
f kiełbasa: kiełbasy kiełbasę
f wołowina: wołowiny wołowinę
f wieprzowina: wieprzowiny wieprzowinę
f pierś: piersi
f mozzarella: mozzarelli mozzarellę
f feta: fety fetę
f czekolada: czekolady czekoladę
f woda: wody wodę
f żelatyna: żelatyny żelatynę
f wanilia: wanilii wanilię
f cukinia: cukinii cukinię cukinie
f dynia: dyni dynię
f kapusta: kapusty kapustę
f sałata: sałaty sałatę
f rukola: rukoli rukolę
f fasola: fasoli fasolę
f fasolka: fasolki fasolkę
f soczewica: soczewicy soczewicę
f ciecierzyca: ciecierzycy ciecierzycę
f kasza: kaszy kaszę
f bułka: bułki bułkę bułek
f bagietka: bagietki bagietkę bagietek
f musztarda: musztardy musztardę
f gałka: gałki gałkę
f skórka: skórki skórkę
f mięta: mięty miętę
f kolendra: kolendry kolendrę
f gruszka: gruszki gruszkę gruszek
f śliwka: śliwki śliwkę śliwek
f truskawka: truskawki truskawkę truskawek
f malina: maliny malinę malin
f borówka: borówki borówkę borówek
f kukurydza: kukurydzy kukurydzę
f tortilla: tortilli tortillę tortille
f pasta: pasty pastę
f przyprawa: przyprawy przyprawę przypraw
f ryba: ryby rybę ryb
f krewetka: krewetki krewetkę krewetek
f pieczarka: pieczarki pieczarkę pieczarek
f oliwka: oliwki oliwkę oliwek
f kawa: kawy kawę
f herbata: herbaty herbatę
f bułka tarta: bułki tartej bułkę tartą
n masło: masła masłem
n mleko: mleka mlekiem
n mleczko: mleczka mleczkiem
n mięso: mięsa mięsem
n wino: wina winem
n piwo: piwa piwem
n ciasto: ciasta ciastem
n ziele: ziela
n jabłko: jabłka jabłek
pl jajka: jajko jajka jajek jaja jaj jajkiem jajkami
pl żółtka: żółtko żółtek żółtkiem żółtkami
pl białka: białko białek białkiem białkami
pl drożdże: drożdży drożdżami
pl płatki: płatków
pl biszkopty: biszkopt biszkoptów
pl rodzynki: rodzynka rodzynek
pl kapary: kapar kaparów
pl ziemniaki: ziemniak ziemniaka ziemniaków
m cukier: cukru cukrem
m cukier puder: cukru pudru
m ser: sera serem sery serów
m twaróg: twarogu
m jogurt: jogurtu jogurty jogurtów
m kefir: kefiru
m czosnek: czosnku czosnkiem
m imbir: imbiru
m cynamon: cynamonu
m pieprz: pieprzu
m majeranek: majeranku
m tymianek: tymianku
m rozmaryn: rozmarynu
m koperek: koperku
m szczypiorek: szczypiorku
m kmin: kminu
m makaron: makaronu
m ryż: ryżu
m chleb: chleba
m boczek: boczku
m kurczak: kurczaka
m filet: fileta filetu filety filetów
m łosoś: łososia
m tuńczyk: tuńczyka
m dorsz: dorsza
m olej: oleju olejem
m ocet: octu
m miód: miodu
m sok: soku
m bulion: bulionu
m proszek: proszku
m pomidor: pomidora pomidory pomidorów
m ogórek: ogórka ogórki ogórków
m groszek: groszku
m szpinak: szpinaku
m brokuł: brokuła brokułu brokuły brokułów
m kalafior: kalafiora
m por: pora pory porów
m seler: selera
m burak: buraka buraki buraków
m banan: banana banany bananów
m ananas: ananasa
m orzech: orzecha orzechy orzechów
m migdał: migdała migdały migdałów
m kuskus: kuskusu
m budyń: budyniu
m dżem: dżemu
m ketchup: ketchupu
m majonez: majonezu
m chrzan: chrzanu
m sos: sosu
m koncentrat: koncentratu
m przecier: przecieru
m parmezan: parmezanu
m serek: serka serki serków
"""

_FORMS: dict[str, tuple[str, str]] = {}  # any form → (canonical, gender)
for _line in _LEXICON.strip().splitlines():
    _head, _, _rest = _line.partition(":")
    _gender, _, _canon = _head.partition(" ")
    if " " in _canon:
        continue  # a phrase – below
    for _form in (_canon, *_rest.split()):
        _FORMS.setdefault(_form, (_canon, _gender))
# multi-word entries ("cukier puder: cukru pudru", "bułka tarta: bułki tartej bułkę tartą") – forms in pairs
_PHRASES: dict[str, str] = {}
for _line in _LEXICON.strip().splitlines():
    _head, _, _rest = _line.partition(":")
    _canon = _head.partition(" ")[2]
    if " " in _canon:
        _words = _rest.split()
        _PHRASES[_canon] = _canon
        for _i in range(0, len(_words) - 1, 2):
            _PHRASES[f"{_words[_i]} {_words[_i + 1]}"] = _canon

# kitchen units: lemma → (1, 2–4, 5+, fraction)
UNIT_WORDS = {
    "łyżka": ("łyżka", "łyżki", "łyżek", "łyżki"),
    "łyżeczka": ("łyżeczka", "łyżeczki", "łyżeczek", "łyżeczki"),
    "szklanka": ("szklanka", "szklanki", "szklanek", "szklanki"),
    "szczypta": ("szczypta", "szczypty", "szczypt", "szczypty"),
    "ząbek": ("ząbek", "ząbki", "ząbków", "ząbka"),
    "pęczek": ("pęczek", "pęczki", "pęczków", "pęczka"),
    "laska": ("laska", "laski", "lasek", "laski"),
    "plaster": ("plaster", "plastry", "plastrów", "plastra"),
    "plasterek": ("plasterek", "plasterki", "plasterków", "plasterka"),
    "kostka": ("kostka", "kostki", "kostek", "kostki"),
    "puszka": ("puszka", "puszki", "puszek", "puszki"),
    "słoik": ("słoik", "słoiki", "słoików", "słoika"),
    "garść": ("garść", "garście", "garści", "garści"),
    "torebka": ("torebka", "torebki", "torebek", "torebki"),
    "główka": ("główka", "główki", "główek", "główki"),
    "gałązka": ("gałązka", "gałązki", "gałązek", "gałązki"),
    "kawałek": ("kawałek", "kawałki", "kawałków", "kawałka"),
    "kromka": ("kromka", "kromki", "kromek", "kromki"),
    "paczka": ("paczka", "paczki", "paczek", "paczki"),
    "butelka": ("butelka", "butelki", "butelek", "butelki"),
    "op": ("op.", "op.", "op.", "op."),
}
UNIT_FORMS: dict[str, str] = {}
for _lemma, _words in UNIT_WORDS.items():
    for _w in (_lemma, *_words):
        UNIT_FORMS[_w.rstrip(".")] = _lemma
for _w in ("opak", "opakowanie", "opakowania", "opakowań", "opakowaniu"):
    UNIT_FORMS[_w] = "op"
UNIT_FORMS |= {"but": "butelka", "łyżkę": "łyżka", "łyżeczkę": "łyżeczka", "szklankę": "szklanka"}

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_TAIL = re.compile(r"(\s*[,(].*|\s+(?:do|z|ze|na|w|we|bez|dla|od|o|i|lub|albo|bądź)\s.*)$", re.IGNORECASE)
# oblique adjective endings (genitive, dative, instrumental, locative; sg and pl) – nominatives are left alone
_ADJ_ENDINGS = ("ego", "emu", "ych", "ymi", "ich", "imi", "iej", "ej", "ym", "im", "ą")


def unit_word(lemma: str, amount: float | str) -> str:
    """Polish agreement: 1 łyżka, 2 łyżki, 5 łyżek, 1,5 łyżki."""
    if isinstance(amount, str):
        amount = float(amount.replace(",", "."))
    one, few, many, frac = UNIT_WORDS[lemma]
    if amount != int(amount):
        return frac
    n = int(amount)
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def split_unit(name: str, qty: str | None) -> tuple[str, str | None]:
    """'Laska cynamonu', qty '1' → ('cynamonu', '1 laska'); 'szczypta soli' → ('soli', '1 szczypta')."""
    first, _, rest = name.partition(" ")
    lemma = UNIT_FORMS.get(first.lower().rstrip("."))
    if not lemma or not rest or (qty and not _NUMBER.fullmatch(qty)):
        return name, qty
    n = qty or "1"
    return rest, f"{n} {unit_word(lemma, n)}"


def _adjective(word: str, gender: str) -> str:
    low = word.lower()
    if len(low) < 5 or not low.isalpha():
        return word
    for end in _ADJ_ENDINGS:
        if low.endswith(end):
            stem = low[: -len(end)]
            if end.startswith("i"):  # słodkiej, polskich, średniej: the i belongs to the stem
                stem += "i"
            break
    else:
        return word
    soft = stem.endswith("i") and not stem.endswith(("ki", "gi"))
    if stem.endswith(("ki", "gi")):
        stem = stem[:-1]
    velar = stem.endswith(("k", "g"))
    if gender == "f":
        return stem + "a"
    if gender == "m":
        return stem if soft else stem + ("i" if velar else "y")
    return stem + ("e" if soft or not velar else "ie")  # n, pl


def canonical(name: str) -> str:
    """'mąki pszennej typ 450' → 'mąka pszenna typ 450', 'jajek' → 'jajka', 'cukru pudru' → 'cukier puder'.

    Lower case; the caller capitalises.
    """
    text = " ".join((name or "").split())
    m = _TAIL.search(text)
    head, tail = (text[: m.start()], m.group(0)) if m and m.start() > 0 else (text, "")
    low = head.lower()
    for phrase, canon in _PHRASES.items():
        if low == phrase or low.startswith(phrase + " "):
            return canon + head[len(phrase) :] + tail
    words = head.split(" ")
    for i, w in enumerate(words):
        hit = _FORMS.get(w.lower())
        if not hit:
            continue
        canon, gender = hit
        if w.lower() == canon:
            break  # already the shop's form – the rest of the words too
        words[i] = canon
        words = [x if j == i else _adjective(x, gender) for j, x in enumerate(words)]
        break
    return " ".join(words) + tail


# case endings cut off words the lexicon does not know: 'rabarbaru' and 'rabarbar' meet as 'rabarbar'
_ENDINGS = ("ami", "ach", "ów", "om", "em", "a", "u", "y", "i", "e", "o", "ę", "ą")


def _stem(word: str) -> str:
    if word in _FORMS or len(word) < 4 or not word.isalpha():
        return word
    for end in _ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 3:
            return word[: -len(end)]
    return word


def key(name: str) -> str:
    """What two list rows are compared by: the canonical name before a comma or bracket, unknown words
    without their case ending ('Rabarbaru' = 'rabarbar', 'Tahini' = 'tahini')."""
    head = re.split(r"\s*[,(]", canonical(name).lower(), maxsplit=1)[0]
    return " ".join(_stem(w) for w in head.split())
