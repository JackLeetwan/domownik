"""💳 Loyalty cards of the house – the barcode shown at the till, drawn here as SVG.

A card has a number and the kind of barcode on the plastic. Scanning it with the phone camera tells both
(the browser's BarcodeDetector); typed by hand, the kind is guessed: 13 / 8 / 12 digits with a valid check
digit are EAN-13 / EAN-8 / UPC-A, anything else Code 128 – which every till reads.

Only static codes: an app that shows a code changing each time (Lidl Plus, Żabka) cannot be kept here.
"""

from __future__ import annotations

import io
import re

import barcode
import segno
from barcode.writer import SVGWriter

# ours → (label, python-barcode name or "qr")
FORMATS: dict[str, tuple[str, str]] = {
    "code128": ("Code 128", "code128"),
    "ean13": ("EAN-13", "ean13"),
    "ean8": ("EAN-8", "ean8"),
    "upca": ("UPC-A", "upca"),
    "code39": ("Code 39", "code39"),
    "itf": ("ITF", "itf"),
    "codabar": ("Codabar", "codabar"),
    "qr": ("QR", "qr"),
}
# what the browser's BarcodeDetector reports → ours
DETECTED = {
    "code_128": "code128",
    "ean_13": "ean13",
    "ean_8": "ean8",
    "upc_a": "upca",
    "code_39": "code39",
    "itf": "itf",
    "codabar": "codabar",
    "qr_code": "qr",
}
MAX_CODE = 120


class CardError(ValueError):
    """Shown to the person as is (Polish)."""


def _gtin_ok(digits: str) -> bool:
    """The check digit of EAN-8 / UPC-A / EAN-13 (weights 3,1,3,… from the right, the check digit excluded)."""
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def guess(code: str) -> str:
    if code.isdigit():
        if len(code) == 13 and _gtin_ok(code):
            return "ean13"
        if len(code) == 8 and _gtin_ok(code):
            return "ean8"
        if len(code) == 12 and _gtin_ok(code):
            return "upca"
    return "code128"


def clean(code: str, fmt: str | None) -> tuple[str, str]:
    """The number as typed or scanned → (code, format). Raises CardError."""
    raw = str(code or "").strip()
    if not raw:
        raise CardError("Wpisz numer karty albo zeskanuj kod")
    fmt = DETECTED.get(fmt or "", fmt or "")
    if fmt not in FORMATS:
        fmt = ""
    # a typed number comes in groups ("2900 1234 5678 9"); a scanned one is exactly what the code holds
    code = raw if fmt else re.sub(r"\s+", "", raw)
    if len(code) > MAX_CODE:
        raise CardError("Ten numer jest za długi jak na kartę")
    if not fmt:
        fmt = guess(code)
    if fmt in ("ean13", "ean8", "upca"):
        n = {"ean13": 13, "ean8": 8, "upca": 12}[fmt]
        if not (code.isdigit() and len(code) == n and _gtin_ok(code)):
            fmt = "code128"  # a mistyped digit: still drawable, the till reads Code 128 too
    if fmt == "code128" and not all(32 <= ord(c) < 127 for c in code):
        raise CardError("Numer karty może mieć tylko cyfry, litery bez polskich znaków i zwykłe symbole")
    try:
        svg(code, fmt)
    except Exception as exc:
        raise CardError(f"Tego numeru nie da się zapisać jako {FORMATS[fmt][0]}") from exc
    return code, fmt


def svg(code: str, fmt: str) -> str:
    """The barcode as an inline <svg> with a viewBox (CSS stretches it to the screen), black on white,
    quiet zone kept, no text under it (the number is printed by the page, big)."""
    if fmt == "qr":
        out = segno.make(code, error="m", micro=False).svg_inline(scale=1, border=4, dark="#000", light="#fff")
        out = out.replace(' class="segno"', "", 1)
        cls = "card-code-qr"
    else:
        buf = io.BytesIO()
        barcode.get(FORMATS[fmt][1], code, writer=SVGWriter()).write(
            buf,
            {"module_width": 0.4, "module_height": 18.0, "quiet_zone": 4.0, "write_text": False, "background": "white"},
        )
        out = buf.getvalue().decode()
        out = out[out.index("<svg") :].replace('mm"', '"')  # mm → plain units of the viewBox below
        cls = "card-code-bars"
    m = re.search(r'<svg[^>]*?\swidth="([\d.]+)" height="([\d.]+)"', out)
    if m is None:
        raise ValueError("unexpected SVG")
    w, h = m.groups()
    head = m.group(0).replace(f'width="{w}" height="{h}"', f'viewBox="0 0 {w} {h}"')
    out = out.replace(m.group(0), head, 1)
    out = re.sub(r"<!--.*?-->", "", out, flags=re.DOTALL)
    return out.replace("<svg ", f'<svg class="{cls}" role="img" aria-label="Kod karty" ', 1)


# ---------------------------------------------------------------- the shop's logo, stored with the house's data

LOGO_DIR = "cards"
_LOGO_NAME = re.compile(r"[0-9a-f]{32}\.png")
UPLOAD_MAX = 8 * 1024 * 1024
OWN_LOGO_BOX = (320, 200)  # a photo of the card itself is wide – kept so


def _root():
    from domownik.config import get_settings

    return get_settings().uploads_dir.resolve()


def logo_file(card):
    """Absolute path of the card's logo, or None – only our own names under uploads/cards/."""
    parts = (card.logo_path or "").split("/")
    if len(parts) != 2 or parts[0] != LOGO_DIR or not _LOGO_NAME.fullmatch(parts[1]):
        return None
    path = (_root() / card.logo_path).resolve()
    return path if path.is_relative_to(_root()) and path.is_file() else None


def drop_logo(card) -> None:
    path = logo_file(card)
    if path is not None:
        path.unlink(missing_ok=True)
    card.logo_path = None


def store_logo(card, png: bytes) -> None:
    import secrets

    drop_logo(card)
    folder = _root() / LOGO_DIR
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.png"
    (folder / name).write_bytes(png)
    card.logo_path = f"{LOGO_DIR}/{name}"


def own_logo(data) -> bytes:
    """An uploaded picture (a logo, a photo of the card) → a clean PNG. Raises CardError."""
    from PIL import Image, ImageOps

    raw = data.read(UPLOAD_MAX + 1)
    if len(raw) > UPLOAD_MAX:
        raise CardError("Obrazek jest za duży (do 8 MB)")
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im).convert("RGBA")
    except Exception as exc:
        raise CardError("To nie jest obrazek – wybierz PNG, JPG albo WebP") from exc
    im.thumbnail(OWN_LOGO_BOX)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)  # re-encoded: nothing of the original file (EXIF, place) is kept
    return out.getvalue()


def apply_brand(card, brand) -> None:
    """What brands.look_up found → the card (a logo of one's own is replaced only by a found one)."""
    if brand is None:
        return
    card.site = brand.site
    if brand.color:
        card.color = brand.color
    if brand.logo:
        store_logo(card, brand.logo)
