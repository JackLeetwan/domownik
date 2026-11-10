"""A shop's logo and colour for its loyalty card – read from the shop's own website.

The name typed on the card ("Rossmann", "IKEA Family") → the site (catalog/seed/stores.yaml, or the site
given on the card, or a guess like rossmann.pl checked against the page title) → its home page: the icons
it declares (apple-touch-icon, the web app manifest, favicon) and its theme colour. A shop that turns
robots away (or has only a tiny icon) gets it from Google's favicon service or icon.horse – only the shop's
address is sent there.

The colour: the site's theme colour when it is a real colour, else the dominant colour of the logo.
Nothing of this is in git – the logo is stored with the house's data when a card is added.
"""

from __future__ import annotations

import colorsys
import io
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from functools import cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml
from PIL import Image

from domownik import webfetch

STORES_FILE = Path(__file__).parent / "catalog" / "seed" / "stores.yaml"
LOGO_PX = 128
MIN_LOGO_PX = 48  # smaller than this looks blurry on a tile – an initial is drawn instead
PAGE_BYTES = 4_000_000
ICON_BYTES = 1_000_000
DEADLINE_S = 20.0  # all of it, so adding a card never hangs
# when the shop's own site has no big enough icon (or turns robots away) – only the shop's address is sent
FALLBACKS = ["https://www.google.com/s2/favicons?domain={domain}&sz=128", "https://icon.horse/icon/{domain}"]


@dataclass
class Brand:
    site: str
    color: str | None  # "#c8102e"
    logo: bytes | None  # PNG, LOGO_PX square, transparent around


# ---------------------------------------------------------------- the site of a shop


def _norm(s: str) -> str:
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", s).split())


@cache
def stores() -> dict[str, str]:
    raw = yaml.safe_load(STORES_FILE.read_text(encoding="utf-8")) or {}
    return {_norm(str(k)): str(v) for k, v in raw.items()}


def host_of(site: str) -> str | None:
    """'https://www.rossmann.pl/promocje' / 'rossmann.pl' → 'rossmann.pl'."""
    site = (site or "").strip()
    if not site:
        return None
    if "://" not in site:
        site = "https://" + site
    host = (urlsplit(site).hostname or "").lower().removeprefix("www.")
    return host if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host) else None


def known_site(name: str) -> str | None:
    """From the list: the exact name, else the longest known name inside it ('Rossmann Club' → rossmann)."""
    n = _norm(name)
    table = stores()
    if n in table:
        return table[n]
    for key in sorted(table, key=len, reverse=True):
        if re.search(rf"(^| ){re.escape(key)}( |$)", n):
            return table[key]
    return None


def _guesses(name: str) -> list[str]:
    slug = _norm(name).replace(" ", "")
    return [f"{slug}.pl", f"{slug}.com"] if 2 < len(slug) <= 30 and slug.isalnum() else []


# ---------------------------------------------------------------- reading the home page


class _Head(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.icons: list[tuple[int, str]] = []  # (declared size, href)
        self.manifest: str | None = None
        self.theme: str | None = None
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "")
            if not href:
                return
            if "manifest" in rel:
                self.manifest = href
            elif "icon" in rel and "mask" not in rel and not href.lower().split("?")[0].endswith(".svg"):
                m = re.search(r"(\d+)x\d+", a.get("sizes", ""))
                size = int(m.group(1)) if m else (180 if "apple-touch" in rel else 32)
                self.icons.append((size, href))
        elif tag == "meta" and a.get("name", "").lower() == "theme-color" and not self.theme:
            self.theme = a.get("content")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 200:
            self.title += data


def _hex(color: str | None) -> str | None:
    c = (color or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{3}", c):
        c = "#" + "".join(ch * 2 for ch in c[1:])
    return c if re.fullmatch(r"#[0-9a-f]{6}", c) else None


def _rgb(hexcolor: str) -> tuple[int, int, int]:
    return int(hexcolor[1:3], 16), int(hexcolor[3:5], 16), int(hexcolor[5:7], 16)


def _brand_like(rgb: tuple[int, int, int]) -> bool:
    """A colour a brand would pick – not white, not black, not grey."""
    _hue, light, sat = colorsys.rgb_to_hls(*(x / 255 for x in rgb))
    return 0.12 < light < 0.9 and sat > 0.25


# ---------------------------------------------------------------- the logo


def _open_icon(data: bytes) -> Image.Image | None:
    try:
        im = Image.open(io.BytesIO(data))
        if im.format == "ICO" and getattr(im, "ico", None):
            im.size = max(im.ico.sizes())  # the biggest picture inside the .ico
        im.load()
        return im.convert("RGBA")
    except Exception:
        return None


def _square(im: Image.Image) -> bytes:
    """Trimmed of transparent margins, fitted into LOGO_PX × LOGO_PX (transparent around), as PNG."""
    box = im.getchannel("A").getbbox()
    if box:
        im = im.crop(box)
    im.thumbnail((LOGO_PX, LOGO_PX), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (LOGO_PX, LOGO_PX), (0, 0, 0, 0))
    out.paste(im, ((LOGO_PX - im.width) // 2, (LOGO_PX - im.height) // 2), im)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def dominant(im: Image.Image) -> str | None:
    """The logo's most common real colour (white backgrounds, black text and greys left out)."""
    small = im.copy()
    small.thumbnail((64, 64))
    raw = small.tobytes()  # RGBA bytes – the same in every Pillow (the per-pixel getter goes in Pillow 14)
    pixels = [tuple(raw[i : i + 4]) for i in range(0, len(raw), 4)]
    counts: dict[tuple[int, int, int], int] = {}
    for r, g, b, a in pixels:
        if a < 200 or not _brand_like((r, g, b)):
            continue
        key = (r // 24 * 24 + 12, g // 24 * 24 + 12, b // 24 * 24 + 12)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    best = max(counts, key=counts.get)
    # the average of the real pixels in that bucket – the bucket centre is a bit off the brand colour
    px = [
        (r, g, b)
        for r, g, b, a in pixels
        if a >= 200 and (r // 24 * 24 + 12, g // 24 * 24 + 12, b // 24 * 24 + 12) == best
    ]
    r, g, b = (round(sum(c[i] for c in px) / len(px)) for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"


def text_on(color: str | None) -> str:
    """White or near-black text on the colour, whichever reads better."""
    if not color:
        return "#fff"
    r, g, b = (x / 255 for x in _rgb(color))
    lum = 0.2126 * r**2.2 + 0.7152 * g**2.2 + 0.0722 * b**2.2
    return "#1b1b1b" if lum > 0.42 else "#fff"


# ---------------------------------------------------------------- all together


def _left(start: float) -> float:
    return DEADLINE_S - (time.monotonic() - start)


def _get(url: str, start: float, max_bytes: int) -> webfetch.Fetched | None:
    left = _left(start)
    if left < 1:
        return None
    try:
        return webfetch.get(url, max_bytes=max_bytes, timeout=min(8.0, left))
    except webfetch.FetchError:
        return None


def look_up(name: str, site: str | None = None) -> Brand | None:
    """The shop's site, logo and colour – None when no site could be found. Never raises."""
    start = time.monotonic()
    page = None
    domain = host_of(site or "") or known_site(name)
    if domain:
        page = _get(f"https://{domain}/", start, PAGE_BYTES)
    else:
        for guess in _guesses(name):
            got = _get(f"https://{guess}/", start, PAGE_BYTES)
            if got is not None and _norm(name).split()[0] in _norm(_title(got.text)):
                domain, page = guess, got
                break
    if not domain:
        return None

    head = _Head()
    base = f"https://{domain}/"
    if page is not None:
        base = page.url
        try:
            head.feed(page.text)
        except Exception:
            pass
    icons = sorted(head.icons, key=lambda x: -x[0])
    theme = _hex(head.theme)
    if head.manifest:
        got = _get(urljoin(base, head.manifest), start, 200_000)
        if got is not None:
            try:
                man = json.loads(got.text)
                for ic in man.get("icons") or []:
                    src = str(ic.get("src") or "")
                    m = re.search(r"(\d+)x\d+", str(ic.get("sizes") or ""))
                    if src and not src.lower().split("?")[0].endswith(".svg"):
                        icons.append((int(m.group(1)) if m else 96, src))
                theme = theme if theme and _brand_like(_rgb(theme)) else _hex(man.get("theme_color"))
            except ValueError, AttributeError, TypeError:
                pass
    icons = sorted({urljoin(base, h): s for s, h in icons}.items(), key=lambda x: -x[1])
    own = [u for u, _ in icons[:3]] + [urljoin(base, "/apple-touch-icon.png")] if page is not None else []
    candidates = own + [f.format(domain=domain) for f in FALLBACKS]

    fallbacks = {f.format(domain=domain) for f in FALLBACKS}
    best: Image.Image | None = None
    seen_color: str | None = None  # the first brand colour in any icon tried (a 16 px favicon has it too)
    for url in candidates:
        got = _get(url, start, ICON_BYTES)
        im = _open_icon(got.body) if got is not None else None
        if im is None:
            continue
        found = dominant(im)
        seen_color = seen_color or found
        if url in fallbacks and found is None:
            continue  # a service's grey stand-in ("K" on grey) – not the shop's logo
        if best is None or min(im.size) > min(best.size):
            best = im
        if min(best.size) >= 96:
            break

    color = theme if theme and _brand_like(_rgb(theme)) else None
    if color is None and best is not None:
        color = dominant(best)
    color = color or seen_color
    logo = _square(best) if best is not None and min(best.size) >= MIN_LOGO_PX else None
    return Brand(site=domain, color=color, logo=logo)


def _title(page: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else ""
