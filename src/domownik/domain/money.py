from __future__ import annotations

from decimal import Decimal, InvalidOperation


class InvalidAmount(ValueError):
    """A money field that is not a number – shown to the user, never a 500."""


MAX_PLN = Decimal(100_000_000)  # far above any household amount; also stops "1e999"


def parse_pln(value: str | None) -> int | None:
    """'1 234,50' / '1234.5' / '' -> grosze (int) or None; anything else -> InvalidAmount."""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "").replace("\u00a0", "").replace("zł", "").replace(",", ".")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or abs(amount) > MAX_PLN:
        raise InvalidAmount(f"Nieprawidłowa kwota: „{value}”. Wpisz liczbę, np. 54,90.")
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
