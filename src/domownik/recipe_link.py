"""A recipe from a link – Cookidoo (Thermomix app → Udostępnij → Kopiuj link) or any recipe site.

Recipe pages carry a schema.org Recipe in JSON-LD (name, ingredients, steps, time, yield) – Cookidoo,
Kwestia Smaku, Ania Gotuje and most others, because search engines read it. No login needed. Recipes one
created oneself in Cookidoo are private and cannot be read this way.

Only public internet addresses are fetched (every redirect checked) – a link cannot reach the home network.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from domownik import webfetch

MAX_BYTES = 3 * 1024 * 1024
TIMEOUT = 15.0
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# cookidoo.pl, cookidoo.de, cookidoo.co.uk, cookidoo.international, cookidoo.thermomix.com …
_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*cookidoo\.[a-z]{2,}(?:\.[a-z]{2,})?$|^cookidoo\.thermomix\.com$")
_LD = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}


class RecipeLinkError(Exception):
    """A message for the user (Polish)."""


@dataclass
class LinkedRecipe:
    name: str
    url: str
    ingredients: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    minutes: int | None = None
    servings: int | None = None
    tags: list[str] = field(default_factory=list)


_BARE = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/\S*", re.IGNORECASE)


def find_url(text: str) -> str:
    """The link in what was pasted or shared – apps share 'Zobacz przepis … https://…' (a Cookidoo one first)."""
    text = (text or "").strip()
    if text and "://" not in text and _BARE.match(text):
        text = "https://" + text
    urls = [m.group(0).rstrip(".,;)") for m in _URL.finditer(text)]
    urls = [u for u in urls if _web(u)]
    if not urls:
        raise RecipeLinkError("Wklej link do przepisu – ze strony albo z Cookidoo (Udostępnij → Kopiuj link)")
    return next((u for u in urls if is_cookidoo(u)), urls[0])


def _web(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def is_cookidoo(url: str) -> bool:
    return _web(url) and bool(_HOST.match((urlsplit(url).hostname or "").lower()))


def site(url: str) -> str:
    """'https://www.kwestiasmaku.com/…' → 'kwestiasmaku' (the tag of an imported recipe)."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    return "cookidoo" if is_cookidoo(url) else host.split(".")[0][:30]


def _check(request: httpx.Request) -> None:
    """Every hop (redirects too) goes to a public internet server – never into the home network."""
    try:
        webfetch.check(request)
    except webfetch.FetchError as exc:
        raise RecipeLinkError(str(exc)) from None


def fetch(url: str) -> str:
    try:
        return webfetch.get(url, max_bytes=MAX_BYTES, timeout=TIMEOUT, what="Strona przepisu").text
    except webfetch.FetchError as exc:
        raise RecipeLinkError(str(exc)) from None


def _recipes_in(data) -> list[dict]:
    if isinstance(data, list):
        return [r for d in data for r in _recipes_in(d)]
    if not isinstance(data, dict):
        return []
    found = []
    kind = data.get("@type")
    if kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind):
        found.append(data)
    if "@graph" in data:
        found += _recipes_in(data["@graph"])
    return found


def _text(v) -> str:
    return " ".join(html.unescape(str(v or "")).split())


def _number(q: str) -> str:
    return f"{q:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def tidy_ingredient(line: str) -> str:
    """'1 ½ TL soli' → '1,5 TL soli', '½ cebuli' → '0,5 cebuli', '1-2 ząbki' → '2 ząbki' (buy enough)."""
    line = _text(line)

    def whole_and_fraction(m: re.Match) -> str:
        return _number(int(m.group(1)) + _FRACTIONS[m.group(2)])

    line = re.sub(r"^(\d+)\s*([½¼¾⅓⅔])", whole_and_fraction, line)
    line = re.sub(r"^([½¼¾⅓⅔])", lambda m: _number(_FRACTIONS[m.group(1)]), line)
    line = re.sub(r"^\d+(?:[.,]\d+)?\s*[-–]\s*(\d+(?:[.,]\d+)?)", r"\1", line)
    return line


def _minutes(iso) -> int | None:
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", str(iso or "").strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0) or None


def _servings(raw) -> int | None:
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    m = re.search(r"\d+", str(raw or ""))
    return int(m.group(0)) if m and 0 < int(m.group(0)) < 1000 else None


def _steps(raw) -> list[str]:
    """recipeInstructions: one text, a list of texts, HowToStep {text} or HowToSection {name, itemListElement}."""
    if isinstance(raw, str):
        text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", html.unescape(raw), flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        return [_text(x) for x in text.splitlines() if _text(x)]
    if isinstance(raw, list):
        return [s for x in raw for s in _steps(x)]
    if isinstance(raw, dict):
        if raw.get("itemListElement"):
            head = [f"{_text(raw.get('name'))}:"] if _text(raw.get("name")) else []
            return head + _steps(raw["itemListElement"])
        return _steps(raw.get("text") or raw.get("name") or "")
    return []


# ---------------------------------------------------------------- pages without JSON-LD

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "dt", "strong", "b", "u", "span", "div", "legend", "summary"}
_SKIP = {"script", "style", "noscript", "svg", "template", "nav", "footer", "form", "button", "select"}
_VOID = {"meta", "link", "br", "img", "input", "hr", "source", "wbr", "area", "base", "col", "embed", "track"}
_ING_LABEL = re.compile(r"^(składniki|skladniki|ingredients|zutaten)\b", re.IGNORECASE)
_STEP_LABEL = re.compile(r"^(przygotowanie|sposób przygotowania|wykonanie|instructions|zubereitung)\b", re.IGNORECASE)


class _Blocks(HTMLParser):
    """The page as a flat list of (tag, itemprop, text) in the order the elements end, plus list ends and
    <meta> values – enough for microdata (itemprop="recipeIngredient") and for 'a list under Składniki'."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[tuple[str, str, str]] = []
        self.meta: dict[str, str] = {}
        self.stack: list[list] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag in _VOID:
            key = a.get("property") or a.get("itemprop") or a.get("name")
            if tag == "meta" and key and "content" in a:
                self.meta.setdefault(key, a["content"])
            return
        if tag in _SKIP or self.skip:
            self.skip += 1
            return
        self.stack.append([tag, a.get("itemprop", ""), []])

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if self.skip:
            self.skip -= 1
            return
        if not any(f[0] == tag for f in self.stack):
            return
        while self.stack:
            name, prop, parts = self.stack.pop()
            text = _text(" ".join(parts))
            if self.stack:
                self.stack[-1][2].append(text)
            if text and (prop or name == "li" or name in _HEADINGS):
                self.out.append((name, prop, text))
            if name in ("ul", "ol"):
                self.out.append(("/list", "", ""))
            if name == tag:
                break

    def handle_data(self, data):
        if not self.skip and self.stack:
            self.stack[-1][2].append(data)


def _list_after(blocks: list[tuple[str, str, str]], label: re.Pattern) -> list[str]:
    """The items of the first list under each heading like 'Składniki' / 'Składniki na krem:'."""
    out: list[str] = []
    state, gap = None, 0  # None | "wait" (heading seen) | "in" (taking items)
    for tag, _prop, text in blocks:
        if tag != "li" and tag != "/list" and len(text) <= 60 and label.match(text):
            state, gap = "wait", 0
        elif state and tag == "li":
            state = "in"
            if re.search(r"[^\W\d_]", text):  # a counter or a page number is not an ingredient
                out.append(text)
        elif state and tag == "/list":
            state = None if state == "in" else state
        elif state == "wait":
            gap += 1
            if gap > 8:  # a heading without a list right under it
                state = None
    return out


def _from_html(page: str, url: str) -> LinkedRecipe | None:
    p = _Blocks()
    try:
        p.feed(page)
        p.close()
    except Exception:  # a broken page is just "no recipe found"
        return None
    blocks = p.out
    ingredients = [t for _, prop, t in blocks if prop in ("recipeIngredient", "ingredients")]
    steps = [t for _, prop, t in blocks if prop == "recipeInstructions"]
    if not ingredients:
        ingredients = _list_after(blocks, _ING_LABEL)
    if not steps:
        steps = _list_after(blocks, _STEP_LABEL)
    if not ingredients:
        return None
    name = next((t for tag, prop, t in blocks if tag == "h1" and len(t) <= 120), "") or _text(
        p.meta.get("og:title", "")
    )
    name = re.split(r"\s+[|–—-]\s+", name)[0] if name else ""
    if not name:
        return None
    return LinkedRecipe(
        name=name[:120],
        url=url,
        ingredients=[tidy_ingredient(x) for x in ingredients][:80],
        steps=steps[:60],
        minutes=_minutes(p.meta.get("totalTime")) or _minutes(p.meta.get("cookTime")),
        servings=_servings(p.meta.get("recipeYield")),
    )


def parse(page: str, url: str) -> LinkedRecipe:
    for block in _LD.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for r in _recipes_in(data):
            name = _text(r.get("name"))[:120]
            raw = r.get("recipeIngredient") or r.get("ingredients") or []
            ingredients = [tidy_ingredient(x) for x in ([raw] if isinstance(raw, str) else raw) if _text(x)]
            if not name:
                continue
            cats = r.get("recipeCategory") or []
            cats = [cats] if isinstance(cats, str) else cats
            return LinkedRecipe(
                name=name,
                url=url,
                ingredients=ingredients[:80],
                steps=_steps(r.get("recipeInstructions"))[:60],
                minutes=_minutes(r.get("totalTime")) or _minutes(r.get("cookTime")),
                servings=_servings(r.get("recipeYield")),
                tags=[_text(c).lower() for c in cats if _text(c)][:3],
            )
    got = _from_html(page, url)
    if got is not None:
        return got
    if is_cookidoo(url):
        raise RecipeLinkError(
            "Nie znalazłem przepisu na tej stronie. Własne przepisy z Twojej kolekcji Cookidoo nie są publiczne"
            " – takie trzeba przepisać ręcznie"
        )
    raise RecipeLinkError("Nie znalazłem przepisu na tej stronie – ta strona nie opisuje go w standardowy sposób")


def load(pasted: str) -> LinkedRecipe:
    url = find_url(pasted)
    return parse(fetch(url), url.split("?")[0].split("#")[0])
