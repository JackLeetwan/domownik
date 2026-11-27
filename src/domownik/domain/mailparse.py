"""Dates out of a kindergarten / school email – plain rules, no language model.

The teacher writes in free Polish ("W piątek tj. 27. listopad odbędzie się nasza wycieczka do teatru",
"03.12 - pieczenie pierników", "26 listopada tj. czwartek sesja zdjęciowa", "dzieci przyszły jutro ubrane
na galowo"). `parse` turns such a text into proposals – a day, maybe an hour, a short title and the paragraph
it came from – and lists what it could not read, so that nothing is lost silently:

- every date-like piece it saw but could not turn into a day is returned in `unread`;
- a weekday written next to a date is checked against the calendar ("27.11 to nie piątek");
- a day before the email was sent, or very far ahead, is flagged;
- a proposal is never written anywhere by this module – a person confirms each one.

Pure functions only: the same text and sending day always give the same result, so the stored email can be
read again after the rules improve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta

MONTHS: tuple[tuple[str, int], ...] = (
    ("stycz", 1),
    ("styczn", 1),
    ("lut", 2),
    ("marc", 3),
    ("marz", 3),
    ("kwie", 4),
    ("kwiet", 4),
    ("maj", 5),
    ("czerw", 6),
    ("lip", 7),
    ("sierp", 8),
    ("wrze", 9),
    ("wrześ", 9),
    ("paźdz", 10),
    ("pazdz", 10),
    ("listop", 11),
    ("grud", 12),
    ("grudz", 12),
)
_MONTH_WORD = (
    r"(?:stycz(?:eń|en|nia|niu)|lut(?:y|ego|ym)|mar(?:zec|ca|cu)|kwie(?:cień|cien|tnia|tniu)|maj(?:a|u)?"
    r"|czerw(?:iec|ca|cu)|lip(?:iec|ca|cu)|sierp(?:ień|ien|nia|niu)|wrze(?:sień|sien|śnia|snia|śniu|sniu)"
    r"|pa[źz]dziernik(?:a|u)?|listopad(?:a|zie)?|grud(?:zień|zien|nia|niu))"
)
WEEKDAYS: tuple[tuple[str, int], ...] = (  # nouns only – "środowy Dzień Niepodległości" is no weekday
    (r"poniedzia[łl](?:ek|ku|ki)", 0),
    (r"wtor(?:ek|ku|ki)", 1),
    (r"[śs]rod(?:a|ę|e|y|zie)", 2),
    (r"czwart(?:ek|ku|ki)", 3),
    (r"pi[ąa]t(?:ek|ku|ki)", 4),
    (r"sobot(?:a|ę|e|y)|sobocie", 5),
    (r"niedziel(?:a|ę|e|i)", 6),
)
WEEKDAY_NAMES = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")
_WEEKDAY_RE = re.compile(r"\b(" + "|".join(p for p, _ in WEEKDAYS) + r")\b", re.I)

# a time: "o godzinie 8.30", "godz. 11.00", "o 9:15", "11:00"; taken out before dates are read (8.30 is no date)
_TIME_RE = re.compile(
    r"(?:\b(?:o\s+)?godz(?:inie|ina|\.)?\s*|\bo\s+(?=\d{1,2}[.:]\d{2}\b)|(?=\b\d{1,2}:\d{2}\b))"
    r"(\d{1,2})(?:[.:](\d{2}))?\b",
    re.I,
)
# "28–30.12", "28-30.12.2026"
_RANGE_NUM_RE = re.compile(r"\b(\d{1,2})\s*[-–—]\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
# "03.12", "3.12.2026", "03/12"
_NUM_RE = re.compile(r"(?<![\d.,])(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?![\d,]|\.\d)")
# "od 28 do 30 grudnia", "28-30 grudnia", "28, 29 i 30 grudnia", "27. listopad", "5 listopada 2026"
_WORD_RE = re.compile(
    r"\b(?:od\s+)?(\d{1,2})\.?((?:\s*(?:,|i|oraz|do|[-–—])\s*\d{1,2}\.?)*)\s+(" + _MONTH_WORD + r")\b(?:\s+(\d{4}))?",
    re.I,
)
_RELATIVE = (
    (re.compile(r"\bpojutrze\b", re.I), 2),
    (re.compile(r"\bjutr(?:o|zejsz\w*)\b", re.I), 1),
    (re.compile(r"\b(?:dzi[śs](?:iaj)?|dzisiejsz\w*)\b", re.I), 0),
)
# a number with a dot that is not a date: amounts, versions – nothing else is flagged as unread
_DATE_LIKE_RE = re.compile(r"(?<![\d.,])\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?(?![\d,])")

FAR_DAYS = 300
PAST_ROLL_DAYS = 60  # "5.01" in a December email means next January; a date up to 2 months back stays this year

# words that say what kind of day it is: icon, and a short title when the sentence is a request, not a name
KEYWORDS: tuple[tuple[re.Pattern[str], str, str | None], ...] = (
    (re.compile(r"galow", re.I), "👔", "Strój galowy"),
    (re.compile(r"teatr", re.I), "🎭", None),
    (re.compile(r"zdj[ęe]ci|fotograf", re.I), "📸", None),
    (re.compile(r"pierni|ciast|piecz", re.I), "🍪", None),
    (re.compile(r"bomb|choink|święt|swiat|wigil|mikołaj|mikolaj|jase", re.I), "🎄", None),
    (re.compile(r"wycieczk|wyjazd|wyjście|wyjscie", re.I), "🚌", None),
    (re.compile(r"przedstawien|występ|wystep|koncert|jase?łk", re.I), "🎭", None),
    (re.compile(r"bal\b|przebier|postaci|kostium", re.I), "🎉", None),
    (re.compile(r"zebrani|spotkani|konsultac", re.I), "👥", None),
    (re.compile(r"zamkni[ęe]t|nieczynn|dyżur|dyzur|przerw", re.I), "🏫", None),
    (re.compile(r"urodzin", re.I), "🎂", None),
)
DEFAULT_ICON = "🏫"
_BRING_RE = re.compile(r"przynie[śs]|zabra[ćc]|zabier|potrzebn|prosimy o\b", re.I)
_FILLER_RE = re.compile(
    r"\b(?:tj\s*\.?|tzn\s*\.?|czyli|odb[ęe]dzie\s+si[ęe]|odbywa\s+si[ęe]|b[ęe]dzie(?:\s+mia[łl]o\s+miejsce)?"
    r"|w\s+dniu|dnia|ju[żz]|nasz[aey]?|naszej|naszych|w\s+najbli[żz]szy|najbli[żz]sz\w*|w\s+dniach|zaplanowan\w*)\b",
    re.I,
)


@dataclass
class Proposal:
    day: date
    end: date  # = day for one day
    at: time | None
    title: str
    icon: str
    sentence: str  # the sentence the date was in
    paragraph: str  # its paragraph – the note of what gets created
    bring: list[str] = field(default_factory=list)  # things to bring, for the shopping list
    warnings: list[str] = field(default_factory=list)

    @property
    def checked(self) -> bool:
        """Ticked by default – unless something about it looks wrong."""
        return not self.warnings


@dataclass
class Result:
    proposals: list[Proposal]
    unread: list[str]  # sentences with something date-like that became no proposal


# ---------------------------------------------------------------- text → paragraphs and sentences


def clean_text(text: str) -> str:
    """Drop quoted replies and signatures; normalise spaces. The teacher's own words stay."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^(?:-- ?|_{5,}|-{5,}\s*(?:Forwarded|Oryginalna|Original).*)$", s):
            break
        if re.match(r"^(?:W dniu|On .+ wrote:|Dnia .+ napisa)", s) and ("napisa" in s or "wrote" in s):
            break
        out.append(re.sub(r"[ \t ]+", " ", s))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def paragraphs(text: str) -> list[str]:
    """Blocks of text; every line that starts with a date ("03.12 - …") is a block of its own."""
    blocks: list[list[str]] = [[]]
    for line in text.split("\n"):
        if not line.strip():
            if blocks[-1]:
                blocks.append([])
            continue
        starts_dated = bool(re.match(r"^\s*(?:[-–•*]\s*)?\d{1,2}(?:[./]\d{1,2}|\.?\s+" + _MONTH_WORD + ")", line, re.I))
        if starts_dated and blocks[-1]:
            blocks.append([])
        blocks[-1].append(line.strip())
    return [" ".join(b) for b in blocks if b]


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ])")


def sentences(par: str) -> list[str]:
    parts = _SENT_SPLIT.split(par)
    # "tj. Czwartek" / "godz. 11" must not end a sentence
    merged: list[str] = []
    for p in parts:
        if merged and re.search(r"\b(?:tj|tzn|godz|ok|ul|np|nr|pt|pn|wt|śr|czw|sob|ndz|dz)\.?$", merged[-1], re.I):
            merged[-1] += " " + p
        else:
            merged.append(p)
    return [m.strip() for m in merged if m.strip()]


# ---------------------------------------------------------------- dates


def _month_of(word: str) -> int | None:
    w = word.lower()
    for stem, m in sorted(MONTHS, key=lambda x: -len(x[0])):
        if w.startswith(stem):
            return m
    return None


def _year(month: int, day: int, explicit: str | None, sent: date) -> int | None:
    if explicit:
        y = int(explicit)
        return y + 2000 if y < 100 else y
    try:
        d = date(sent.year, month, day)
    except ValueError:
        return sent.year  # validity is checked by the caller with this year
    return sent.year + 1 if d < sent - timedelta(days=PAST_ROLL_DAYS) else sent.year


def _mk(day: int, month: int, explicit_year: str | None, sent: date) -> date | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    y = _year(month, day, explicit_year, sent)
    try:
        return date(y, month, day)
    except ValueError:
        return None


@dataclass
class _Found:
    start: int
    stop: int
    days: list[date]  # one, or every day of a list ("28, 29 i 30 grudnia")
    is_range: bool


def _find_dates(s: str, sent: date) -> tuple[list[_Found], list[str]]:
    """Dates in one sentence (times already blanked out). Returns found and the bits that failed."""
    found: list[_Found] = []
    bad: list[str] = []
    taken = [False] * len(s)

    def free(a: int, b: int) -> bool:
        return not any(taken[a:b])

    def take(a: int, b: int) -> None:
        for i in range(a, b):
            taken[i] = True

    for m in _RANGE_NUM_RE.finditer(s):
        a, b = _mk(int(m[1]), int(m[3]), m[4], sent), _mk(int(m[2]), int(m[3]), m[4], sent)
        if a and b and a <= b:
            found.append(_Found(m.start(), m.end(), [a, b], True))
        else:
            bad.append(m[0])
        take(m.start(), m.end())
    for m in _WORD_RE.finditer(s):
        if not free(m.start(), m.end()):
            continue
        month = _month_of(m[3])
        nums = [int(m[1]), *(int(x) for x in re.findall(r"\d{1,2}", m[2] or ""))]
        is_range = bool(re.search(r"do|[-–—]", m[2] or "")) and len(nums) == 2
        days = [_mk(n, month, m[4], sent) for n in nums] if month else [None]
        if all(days):
            found.append(_Found(m.start(), m.end(), sorted(days), is_range))  # type: ignore[arg-type]
        else:
            bad.append(m[0])
        take(m.start(), m.end())
    for m in _NUM_RE.finditer(s):
        if not free(m.start(), m.end()):
            continue
        d = _mk(int(m[1]), int(m[2]), m[3], sent)
        if d:
            found.append(_Found(m.start(), m.end(), [d], False))
        else:
            bad.append(m[0])
        take(m.start(), m.end())
    if not found:
        for rx, plus in _RELATIVE:
            m = rx.search(s)
            if m:
                found.append(_Found(m.start(), m.end(), [sent + timedelta(days=plus)], False))
                break
    found.sort(key=lambda f: f.start)
    return found, bad


def _times(s: str) -> tuple[str, list[time]]:
    """Blank times out of the sentence (so "8.30" is no 8 March) and return them."""
    out: list[time] = []

    def repl(m: re.Match[str]) -> str:
        h, mi = int(m[1]), int(m[2] or 0)
        if 0 <= h <= 23 and 0 <= mi <= 59:
            out.append(time(h, mi))
        return " " * len(m[0])

    return _TIME_RE.sub(repl, s), out


def _weekday_in(s: str) -> int | None:
    m = _WEEKDAY_RE.search(s)
    if not m:
        return None
    w = m[1].lower()
    for pat, n in WEEKDAYS:
        if re.fullmatch(pat, w, re.I):
            return n
    return None


# ---------------------------------------------------------------- title, icon, things to bring


def _title(sentence: str, spans: list[tuple[int, int]], subject: str) -> str:
    s = list(sentence)
    for a, b in spans:
        for i in range(a, b):
            s[i] = " "
    t = "".join(s)
    t = _WEEKDAY_RE.sub(" ", t)
    for rx, _ in _RELATIVE:
        t = rx.sub(" ", t)
    t = re.sub(r"\(.*?\)|\(.*$", " ", t)  # "(ciasto przyniesie mama …)" goes to the note
    t = re.sub(r"[:;]-?[)(DPp]|[\U0001F300-\U0001FAFF\u2600-\u27BF]", " ", t)  # emoticons
    t = re.split(
        r",\s*(?:na\s+kt[óo]r\w+|kt[óo]r\w+|gdzie|ponieważ|poniewaz|dlatego|aby|żeby|zeby|wi[ęe]c|zatem)\b",
        t,
        maxsplit=1,
        flags=re.I,
    )[0]
    t = _FILLER_RE.sub(" ", t)
    t = re.sub(r"^\W*(?:w|we|na|do|od)\b(?=\W*$)", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,:;-–—!?")
    t = re.sub(r"^(?:w|we|na|o|i|a|,)\s+", "", t, flags=re.I).strip(" .,:;-–—")
    t = re.sub(r"\s+(?:do|na|w|we|od|z|o|i|dla)$", "", t, flags=re.I).strip(" .,:;-–—")
    if len(t) < 3:
        t = subject.strip() or "Przedszkole"
    t = t[0].upper() + t[1:]
    if len(t) > 80:
        t = t[:80].rsplit(" ", 1)[0] + "…"
    return t


def _bring(par: str) -> list[str]:
    """'dzieci mogą przynieść posypki, pisaki i będzie nam potrzebny cukier puder aby zrobić lukier'
    → ['posypki', 'pisaki', 'cukier puder']. Only a suggestion – the field is editable."""
    things: list[str] = []
    for m in re.finditer(
        r"(?:przynie[śs][ćc]|zabra[ćc]|potrzebn[yaei]\w*(?:\s+(?:b[ęe]d[ąz]ie|jest|s[ąa]))?)\s+(?:nam\s+)?(.+?)"
        r"(?=\s+(?:aby|żeby|zeby|by|do\s+|na\s+|w\s+|dla\s+|ponieważ|bo|gdyż)\b|[.;:()!?]|$)",
        par,
        re.I,
    ):
        for part in re.split(r",|\s+i\s+|\s+oraz\s+|\s+lub\s+|\s+albo\s+", m[1]):
            p = part.strip(" .,-–")
            p = re.sub(r"^(?:(?:b[ęe]dzie|nam|potrzebn\w*|tak[żz]e|te[żz]|r[óo]wnie[żz])\s+)+", "", p, flags=re.I)
            if p and len(p) <= 40 and not re.search(r"\b(?:b[ęe]dzie|mog[ąa]|nam)\b", p, re.I) and p not in things:
                things.append(p)
    return things[:8]


def _icon(text: str) -> tuple[str, str | None]:
    for rx, icon, title in KEYWORDS:
        if rx.search(text):
            return icon, title
    return DEFAULT_ICON, None


# ---------------------------------------------------------------- the whole email


def parse(subject: str, body: str, sent: date) -> Result:
    text = clean_text(body)
    proposals: list[Proposal] = []
    unread: list[str] = []
    for par in paragraphs(text):
        sents = sentences(par)
        bring = _bring(par) if _BRING_RE.search(par) else []
        times_in_par = [t for s in sents for t in _times(s)[1]]
        for sent_text in sents:
            blanked, times = _times(sent_text)
            found, bad = _find_dates(blanked, sent)
            if bad:
                unread.append(sent_text)
            if not found:
                if _weekday_in(sent_text) is not None and re.search(
                    r"\b(?:w|we|ten|ta|najbli[żz]sz\w*)\s+\w+", sent_text, re.I
                ):
                    unread.append(sent_text)  # "w piątek …" with no date – a person must read it
                continue
            wd = _weekday_in(sent_text)
            spans = [(f.start, f.stop) for f in found] + [(m.start(), m.end()) for m in _TIME_RE.finditer(sent_text)]
            icon, short = _icon(sent_text)
            request = re.match(r"^\s*(?:prosz[ęe]|prosimy|uprzejmie)", sent_text, re.I)
            title = short if short and request else _title(sent_text, spans, subject)
            for f in found:
                groups = [f.days] if f.is_range else [[d] for d in f.days]
                consecutive = len(f.days) > 1 and all(
                    (b - a).days == 1 for a, b in zip(f.days, f.days[1:], strict=False)
                )
                if consecutive and not f.is_range:
                    groups = [[f.days[0], f.days[-1]]]
                for g in groups:
                    day, end = g[0], g[-1]
                    at = (times or [None])[0] if day == end else None
                    if at is None and day == end and len(found) == 1 and len(times_in_par) == 1:
                        at = times_in_par[0]
                    warns: list[str] = []
                    if wd is not None and len(found) == 1 and day == end and day.weekday() != wd:
                        warns.append(
                            f"W mailu stoi {WEEKDAY_NAMES[wd]}, a {day:%d.%m} to {WEEKDAY_NAMES[day.weekday()]} – sprawdź datę"
                        )
                    if day < sent:
                        warns.append("Data przed wysłaniem maila – pewnie już było")
                    if (day - sent).days > FAR_DAYS:
                        warns.append("Data bardzo daleko – sprawdź rok")
                    proposals.append(Proposal(day, end, at, title, icon, sent_text, par, list(bring), warns))
    # one day in the whole email under a short heading ("Dzień Postaci z Bajek"): the heading names it
    pars = paragraphs(text)
    heading = (
        pars[0] if pars and len(pars[0]) <= 60 and not re.search(r"[.,:!?]$|^dzie[ńn] dobry", pars[0], re.I) else None
    )
    if heading and len(proposals) == 1 and heading != proposals[0].paragraph:
        proposals[0].title = heading
        proposals[0].icon = _icon(heading + " " + proposals[0].sentence)[0]
    # the subject alone ("5 listopad") adds a day only when the body named none
    subj_found, _ = _find_dates(_times(subject)[0], sent)
    if subj_found and not proposals:
        for f in subj_found:
            icon, _short = _icon(subject + " " + text)
            for d in f.days:
                proposals.append(Proposal(d, d, None, subject.strip()[:80] or "Przedszkole", icon, subject, text[:600]))
    # the same day twice (subject and body, or two sentences): keep the first, join the rest into its note
    out: list[Proposal] = []
    for p in proposals:
        twin = next((q for q in out if q.day == p.day and q.end == p.end and q.paragraph == p.paragraph), None)
        if twin:
            twin.at = twin.at or p.at
            continue
        out.append(p)
    out.sort(key=lambda p: (p.day, p.at or time(0)))
    return Result(out, list(dict.fromkeys(unread)))


def find_date_like(text: str) -> list[str]:
    """Every bit of text that looks like a date – used by tests to prove nothing slips by unread."""
    return _DATE_LIKE_RE.findall(text)
