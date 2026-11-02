from datetime import timedelta

from sqlalchemy import select

from domownik.config import today
from domownik.models import BoardNote


def test_notes_lifecycle(auth_client, db):
    auth_client.post("/setup/zones")
    auth_client.post("/notatki", data={"body": "Wi-Fi gości: dom123", "pinned": "on"})
    auth_client.post(
        "/notatki", data={"body": "Odbiór paczki", "valid_until": (today() - timedelta(days=1)).isoformat()}
    )
    page = auth_client.get("/notatki").text
    main, archive = page.split("Archiwum (1)", 1)
    assert "Wi-Fi gości" in main and "Odbiór paczki" in archive  # expired → archive
    dash = auth_client.get("/").text
    assert "📌 Wi-Fi gości" in dash and "Odbiór paczki" not in dash.split("<main", 1)[1]

    n = db.scalar(select(BoardNote).where(BoardNote.body.like("Wi-Fi%")))
    auth_client.post(f"/notatki/{n.id}/edit", data={"body": "Wi-Fi gości: nowe hasło"})
    db.refresh(n)
    assert n.body == "Wi-Fi gości: nowe hasło" and n.pinned is False
    auth_client.post(f"/notatki/{n.id}/pin")
    auth_client.post(f"/notatki/{n.id}/archive")
    db.refresh(n)
    assert n.pinned and n.archived
    auth_client.post(f"/notatki/{n.id}/archive")  # restore
    db.refresh(n)
    assert not n.archived
    old = db.scalar(select(BoardNote).where(BoardNote.body == "Odbiór paczki"))
    auth_client.post(f"/notatki/{old.id}/archive")  # restoring an expired note drops the date
    db.refresh(old)
    assert old.valid_until is None and old.is_active(today())
    auth_client.post(f"/notatki/{old.id}/delete")
    db.expunge_all()
    assert db.scalar(select(BoardNote).where(BoardNote.body == "Odbiór paczki")) is None


def test_sidebar_and_bottombar(auth_client, db):
    auth_client.post("/setup/zones")
    auth_client.post("/zakupy", data={"text": "mleko, chleb"})
    page = auth_client.get("/zakupy").text
    side = page.split('<aside class="sidebar"', 1)[1].split("</aside>", 1)[0]
    assert "Samochody" in side and "Wyloguj" in side and '<span class="count">2</span>' in side
    assert 'class="bottombar"' in page and "data-nav-toggle" in page
    # timeline first on the dashboard, tool tiles after it
    dash = auth_client.get("/").text.split("<main", 1)[1]
    assert dash.index('id="timeline"') < dash.index('class="tools"') < dash.index("Najbliższe terminy")


def test_login_page_has_no_sidebar(client):
    page = client.get("/login").text
    assert 'class="sidebar"' not in page and "theme-toggle" in page
