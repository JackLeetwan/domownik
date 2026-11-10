"""The menu: links of the sidebar / "Więcej" drawer and the two own shortcuts of the phone's bottom bar."""

from __future__ import annotations

from dataclasses import dataclass

from domownik.models import User


@dataclass(frozen=True)
class Link:
    key: str
    href: str
    icon: str
    label: str
    count: str | None = None  # Nav attribute with a badge number
    fixed: bool = False  # cannot be hidden


LINKS: tuple[Link, ...] = (
    Link("pulpit", "/", "🏠", "Pulpit", fixed=True),
    Link("kalendarz", "/kalendarz", "📅", "Kalendarz"),
    Link("grafik", "/grafik", "🗓️", "Grafik"),
    Link("obowiazki", "/obowiazki", "🧹", "Obowiązki", "chores"),
    Link("zakupy", "/zakupy", "🛒", "Zakupy", "shopping"),
    Link("kuchnia", "/kuchnia", "🍳", "Kuchnia"),
    Link("notatki", "/notatki", "📝", "Notatki", "notes"),
    Link("zadania", "/zadania", "📋", "Zadania"),
    Link("do-zrobienia", "/do-zrobienia", "☐", "Do zrobienia", "todos"),
    Link("punkty", "/punkty", "⭐", "Punkty", "points"),
    Link("forma", "/forma", "💪", "Forma"),
    Link("zdjecia", "/zdjecia", "📥", "Do rozpoznania", "inbox"),
    Link("kontakty", "/kontakty", "📇", "Kontakty"),
    Link("budzet", "/budzet", "💰", "Budżet"),
    Link("na-wypadek", "/na-wypadek", "🧰", "Na wypadek"),
    Link("ustawienia", "/ustawienia", "⚙️", "Ustawienia", fixed=True),
)
BY_KEY = {link.key: link for link in LINKS}
DEFAULT_BAR = ("zakupy", "notatki")
BAR_CHOICES = tuple(link for link in LINKS if link.key not in ("pulpit", "ustawienia"))


def bar(user: User | None) -> list[Link]:
    keys = list((user.menu or {}).get("bar") or DEFAULT_BAR) if user else list(DEFAULT_BAR)
    picked = [BY_KEY[k] for k in keys if k in BY_KEY and BY_KEY[k] in BAR_CHOICES][:2]
    for k in DEFAULT_BAR:  # always two
        if len(picked) < 2 and BY_KEY[k] not in picked:
            picked.append(BY_KEY[k])
    return picked


def hidden(user: User | None) -> set[str]:
    if not user:
        return set()
    return {k for k in (user.menu or {}).get("hidden") or [] if k in BY_KEY and not BY_KEY[k].fixed}


def sidebar(user: User | None) -> list[Link]:
    off = hidden(user)
    return [link for link in LINKS if link.key not in off]


def save(user: User, form) -> None:
    keys = [str(form.get("bar1") or ""), str(form.get("bar2") or "")]
    keys = [k for k in dict.fromkeys(keys) if k in BY_KEY and BY_KEY[k] in BAR_CHOICES]
    shown = set(form.getlist("show"))
    off = sorted(link.key for link in LINKS if not link.fixed and link.key not in shown and link.key not in keys)
    user.menu = {"bar": keys or list(DEFAULT_BAR), "hidden": off}
