"""Own menu: two bottom-bar shortcuts and hidden sidebar links, per account."""

from __future__ import annotations

from domownik import menu
from domownik.models import User


def _bar_links(html: str) -> list[str]:
    bar = html.split('<nav class="bottombar"')[1].split("</nav>")[0]
    return [h.split('"')[0] for h in bar.split('<a href="')[1:]]


def test_default_and_saved_menu(auth_client, db, user):
    page = auth_client.get("/ustawienia").text
    assert 'id="menu"' in page
    assert _bar_links(auth_client.get("/notatki").text) == ["/", "/zakupy", "/notatki"]

    r = auth_client.post(
        "/ustawienia/menu",
        data={"bar1": "kuchnia", "bar2": "kalendarz", "show": ["zakupy", "notatki", "budzet"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.refresh(user)
    assert user.menu["bar"] == ["kuchnia", "kalendarz"]
    assert "grafik" in user.menu["hidden"] and "kuchnia" not in user.menu["hidden"]  # a shortcut is never hidden

    html = auth_client.get("/notatki").text
    assert _bar_links(html) == ["/", "/kuchnia", "/kalendarz"]
    side = html.split('<ul class="side-links">')[1].split("</ul>")[0]
    assert 'href="/grafik"' not in side and 'href="/budzet"' in side and 'href="/ustawienia"' in side


def test_bad_values_fall_back():
    u = User(
        name="x",
        email="x@x",
        password_hash="h",
        menu={"bar": ["nope", "nope", "ustawienia"], "hidden": ["pulpit", "x"]},
    )
    assert [link.key for link in menu.bar(u)] == ["zakupy", "notatki"]
    assert menu.hidden(u) == set()
    u.menu = {"bar": ["kuchnia"]}
    assert [link.key for link in menu.bar(u)] == ["kuchnia", "zakupy"]
