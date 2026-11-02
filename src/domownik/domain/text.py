"""Text matching for the search box: case- and diacritics-insensitive ("zoltko" finds "Żółtko")."""

from __future__ import annotations

import unicodedata

from markupsafe import Markup, escape

# letters that do not decompose into base + combining mark
_EXTRA = str.maketrans({"ł": "l", "Ł": "l", "đ": "d", "ø": "o", "æ": "ae", "œ": "oe"})


def _fold_char(c: str) -> str:
    c = c.translate(_EXTRA)
    return "".join(ch for ch in unicodedata.normalize("NFKD", c) if not unicodedata.combining(ch)).casefold()


def fold(text: str) -> str:
    """Lower-case without Polish (and other) diacritics."""
    return "".join(_fold_char(c) for c in text or "")


def _fold_map(text: str) -> tuple[str, list[int]]:
    """Folded text plus, for every folded character, the index of the original character it came from."""
    out: list[str] = []
    idx: list[int] = []
    for i, c in enumerate(text):
        f = _fold_char(c)
        out.append(f)
        idx.extend([i] * len(f))
    return "".join(out), idx


def terms(query: str) -> list[str]:
    """Search words, folded; every one of them has to match."""
    return [t for t in fold(query).split() if t]


def matches(text: str, words: list[str]) -> bool:
    f = fold(text)
    return bool(words) and all(w in f for w in words)


def spans(text: str, words: list[str]) -> list[tuple[int, int]]:
    """Character ranges of the original text matched by the words (merged, sorted)."""
    folded, idx = _fold_map(text)
    found: list[tuple[int, int]] = []
    for w in words:
        start = folded.find(w)
        while start != -1:
            found.append((idx[start], idx[start + len(w) - 1] + 1))
            start = folded.find(w, start + len(w))
    found.sort()
    merged: list[tuple[int, int]] = []
    for a, b in found:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def snippet(text: str, words: list[str], width: int = 90) -> Markup:
    """A piece of the text around the first match, matches wrapped in <mark>, everything else escaped."""
    text = " ".join((text or "").split())
    hits = spans(text, words)
    if not hits:
        cut = text[:width]
        return escape(cut) + ("…" if len(text) > width else "")
    first = hits[0][0]
    lo = max(0, first - width // 3)
    if lo:
        # start at a word boundary
        sp = text.rfind(" ", 0, lo + 1)
        lo = sp + 1 if sp != -1 and lo - sp < 15 else lo
    hi = min(len(text), lo + width)
    out = Markup("…" if lo else "")
    pos = lo
    for a, b in hits:
        if b <= lo or a >= hi:
            continue
        a, b = max(a, lo), min(b, hi)
        out += escape(text[pos:a]) + Markup("<mark>") + escape(text[a:b]) + Markup("</mark>")
        pos = b
    out += escape(text[pos:hi])
    if hi < len(text):
        out += Markup("…")
    return out
