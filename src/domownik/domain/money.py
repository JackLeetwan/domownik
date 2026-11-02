from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_pln(value: str | None) -> int | None:
    """'1 234,50' / '1234.5' / '' -> grosze (int) or None."""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "").replace(" ", "").replace("zł", "").replace(",", ".")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Nieprawidłowa kwota: {value!r}") from exc
    return int((amount * 100).quantize(Decimal(1)))


def format_pln(grosze: int | None, *, sign: bool = False, unit: bool = True) -> str:
    if grosze is None:
        return ""
    neg = grosze < 0
    zl, gr = divmod(abs(grosze), 100)
    whole = f"{zl:,}".replace(",", " ")
    text = whole if gr == 0 else f"{whole},{gr:02d}"
    if neg:
        text = "−" + text
    elif sign and grosze > 0:
        text = "+" + text
    return f"{text} zł" if unit else text


def input_pln(grosze: int | None) -> str:
    """Value for an <input> field."""
    if grosze is None:
        return ""
    zl, gr = divmod(grosze, 100)
    return str(zl) if gr == 0 else f"{zl},{gr:02d}"
