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
    group: str = ""  # a heading of the sidebar ("" = no heading: Pulpit on top, Ustawienia at the bottom)


LINKS: tuple[Link, ...] = (
    Link("pulpit", "/", "🏠", "Pulpit", fixed=True),
    Link("kalendarz", "/kalendarz", "📅", "Kalendarz", group="Plan"),
    Link("grafik", "/grafik", "🗓️", "Grafik", group="Plan"),
    Link("zadania", "/zadania", "📋", "Zadania", group="Plan"),
    Link("do-zrobienia", "/do-zrobienia", "☐", "Do zrobienia", "todos", group="Plan"),
    Link("zakupy", "/zakupy", "🛒", "Zakupy", "shopping", group="Dom"),
    Link("kuchnia", "/kuchnia", "🍳", "Kuchnia", group="Dom"),
    Link("obowiazki", "/obowiazki", "🧹", "Obowiązki", "chores", group="Dom"),
    Link("notatki", "/notatki", "📝", "Notatki", "notes", group="Dom"),
    Link("punkty", "/punkty", "⭐", "Punkty", "points", group="Dom"),
    Link("budzet", "/budzet", "💰", "Budżet", group="Pieniądze i papiery"),
    Link("zdjecia", "/zdjecia", "📥", "Do rozpoznania", "inbox", group="Pieniądze i papiery"),
    Link("kontakty", "/kontakty", "📇", "Kontakty", group="Pieniądze i papiery"),
    Link("na-wypadek", "/na-wypadek", "🧰", "Na wypadek", group="Pieniądze i papiery"),
    Link("forma", "/forma", "💪", "Forma", group="Ja"),
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


def sidebar_groups(user: User | None) -> list[tuple[str, list[Link]]]:
    """The sidebar in runs of the same heading, in LINKS order: [("", [Pulpit]), ("Plan", […]), …]."""
    out: list[tuple[str, list[Link]]] = []
    for link in sidebar(user):
        if out and out[-1][0] == link.group:
            out[-1][1].append(link)
        else:
            out.append((link.group, [link]))
    return out


def save(user: User, form) -> None:
    keys = [str(form.get("bar1") or ""), str(form.get("bar2") or "")]
    keys = [k for k in dict.fromkeys(keys) if k in BY_KEY and BY_KEY[k] in BAR_CHOICES]
    shown = set(form.getlist("show"))
    off = sorted(link.key for link in LINKS if not link.fixed and link.key not in shown and link.key not in keys)
    user.menu = {"bar": keys or list(DEFAULT_BAR), "hidden": off}
