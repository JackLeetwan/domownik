"""Recipe from a Cookidoo link (Thermomix app → Udostępnij → Kopiuj link).

Public Cookidoo recipe pages carry a schema.org Recipe in JSON-LD (name, ingredients, time, yield) –
no login needed. Recipes the user created themselves are private and cannot be read this way.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 3 * 1024 * 1024
TIMEOUT = 15.0
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# cookidoo.pl, cookidoo.de, cookidoo.co.uk, cookidoo.international, cookidoo.thermomix.com …
_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*cookidoo\.[a-z]{2,}(?:\.[a-z]{2,})?$|^cookidoo\.thermomix\.com$")
_LD = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}


class CookidooError(Exception):
    """A message for the user (Polish)."""


@dataclass
class CookidooRecipe:
    name: str
    url: str
    ingredients: list[str] = field(default_factory=list)
    minutes: int | None = None
    servings: int | None = None
    tags: list[str] = field(default_factory=list)


def find_url(text: str) -> str:
    """The Cookidoo link in what was pasted – the app shares 'Zobacz przepis … https://cookidoo.pl/…'."""
    text = (text or "").strip()
    if text and "://" not in text and text.lower().startswith(("cookidoo.", "www.cookidoo.")):
        text = "https://" + text
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(".,;)")
        if is_cookidoo(url):
            return url
    raise CookidooError("To nie jest link z Cookidoo – w aplikacji wybierz przy przepisie „Udostępnij” → „Kopiuj link”")


def is_cookidoo(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(_HOST.match((parts.hostname or "").lower()))


def _check(request: httpx.Request) -> None:
    """Every hop (redirects too) stays on a public Cookidoo server."""
    host = request.url.host
    if not is_cookidoo(str(request.url)):
        raise CookidooError("Link przekierowuje poza Cookidoo")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise CookidooError("Nie mogę połączyć się z Cookidoo – sprawdź internet") from None
    if any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise CookidooError("Link prowadzi do sieci lokalnej")


def fetch(url: str) -> str:
    try:
        with (
            httpx.Client(
                timeout=TIMEOUT,
                follow_redirects=True,
                max_redirects=5,
                headers={"User-Agent": "Mozilla/5.0 (Domownik recipe import)", "Accept-Language": "pl-PL,pl"},
                trust_env=False,
                event_hooks={"request": [_check]},
            ) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code == 404:
                raise CookidooError("Cookidoo nie zna takiego przepisu (404) – sprawdź link")
            if resp.status_code >= 400:
                raise CookidooError(f"Cookidoo odpowiedziało błędem {resp.status_code}")
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise CookidooError("Strona przepisu jest podejrzanie duża")
                chunks.append(chunk)
            return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as exc:
        raise CookidooError(f"Nie udało się pobrać przepisu z Cookidoo ({type(exc).__name__})") from None


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


def parse(page: str, url: str) -> CookidooRecipe:
    for block in _LD.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for r in _recipes_in(data):
            name = _text(r.get("name"))[:120]
            ingredients = [tidy_ingredient(x) for x in r.get("recipeIngredient") or [] if _text(x)]
            if not name:
                continue
            cats = r.get("recipeCategory") or []
            cats = [cats] if isinstance(cats, str) else cats
            return CookidooRecipe(
                name=name,
                url=url,
                ingredients=ingredients,
                minutes=_minutes(r.get("totalTime")) or _minutes(r.get("cookTime")),
                servings=_servings(r.get("recipeYield")),
                tags=[_text(c).lower() for c in cats if _text(c)][:3],
            )
    raise CookidooError(
        "Nie znalazłem przepisu na tej stronie. Własne przepisy z Twojej kolekcji Cookidoo nie są publiczne"
        " – takie trzeba przepisać ręcznie"
    )


def load(pasted: str) -> CookidooRecipe:
    url = find_url(pasted)
    return parse(fetch(url), url.split("?")[0].split("#")[0])
