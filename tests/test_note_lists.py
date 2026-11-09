"""Notes: title, checklist with ticking in place, colour, label filter, search, dashboard preview."""

from __future__ import annotations

from sqlalchemy import select

from domownik.models import BoardNote
from domownik.routers.board import parse_items

HX = {"HX-Request": "true"}


def _add(auth_client, **data):
    r = auth_client.post("/notatki", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def test_parse_items():
    assert parse_items("mleko\n✓ chleb\n- [x] masło\n\n  • jajka ") == [
        {"t": "mleko", "d": False},
        {"t": "chleb", "d": True},
        {"t": "masło", "d": True},
        {"t": "jajka", "d": False},
    ]


def test_list_note_tick_add_clear(auth_client, db):
    _add(auth_client, title="Coroczne", body="Październik – ubezpieczenie\nWrzesień – podatek", as_list="1")
    n = db.scalar(select(BoardNote))
    assert n.is_list and n.body == "" and len(n.items) == 2

    page = auth_client.get("/notatki").text
    assert "Coroczne" in page and f'hx-post="/notatki/{n.id}/punkt/1"' in page and "0/2" in page

    r = auth_client.post(f"/notatki/{n.id}/punkt/1", headers=HX)
    assert r.status_code == 200 and f'id="note-{n.id}"' in r.text and "1/2" in r.text
    db.refresh(n)
    assert n.items[1]["d"] is True

    r = auth_client.post(f"/notatki/{n.id}/punkt", data={"t": "Grudzień – urodziny"}, headers=HX)
    assert "Grudzień – urodziny" in r.text
    r = auth_client.post(f"/notatki/{n.id}/wyczysc", headers=HX)
    db.refresh(n)
    assert [i["t"] for i in n.items] == ["Październik – ubezpieczenie", "Grudzień – urodziny"]
    assert auth_client.post(f"/notatki/{n.id}/punkt/9", headers=HX).status_code == 404


def test_edit_switches_list_back_to_text_and_keeps_ticks(auth_client, db):
    _add(auth_client, body="a\nb", as_list="1")
    n = db.scalar(select(BoardNote))
    page = auth_client.get("/notatki").text
    assert ">☐ a\n☐ b</textarea>" in page  # list shown as lines in the edit form, open ones marked ☐
    auth_client.post(f"/notatki/{n.id}/edit", data={"body": "✓ a\nb", "as_list": "1"})
    db.refresh(n)
    assert n.items == [{"t": "a", "d": True}, {"t": "b", "d": False}]
    auth_client.post(f"/notatki/{n.id}/edit", data={"body": "zwykły tekst"})
    db.refresh(n)
    assert n.items is None and n.body == "zwykły tekst"


def test_empty_note_is_refused(auth_client, db):
    r = auth_client.post("/notatki", data={"body": "  ", "as_list": "1"}, headers=HX)
    assert r.status_code == 400
    assert db.scalar(select(BoardNote)) is None


def test_title_only_note_colour_and_label_filter(auth_client, db):
    _add(auth_client, title="Wi-Fi gości", color="blue", label="Dom")
    _add(auth_client, body="Paczka w piątek", color="nope")
    a, b = db.scalars(select(BoardNote).order_by(BoardNote.id)).all()
    assert (a.color, a.label, a.body) == ("blue", "Dom", "")
    assert b.color is None and b.label is None

    page = auth_client.get("/notatki").text
    assert "c-blue" in page and "Bez kategorii" in page and 'href="/notatki?k=Dom"' in page
    only = auth_client.get("/notatki?k=Dom").text
    assert "Wi-Fi gości" in only and "Paczka w piątek" not in only.split("notes-grid")[1]
    none = auth_client.get("/notatki?k=_brak").text
    assert "Paczka w piątek" in none and "Wi-Fi gości" not in none.split("notes-grid")[1]


def test_search_and_dashboard_see_list_points(auth_client, db):
    from domownik.models import Item

    db.add(Item(name="Dom", kind="house"))  # without any zone the dashboard is the welcome screen
    db.commit()
    _add(auth_client, title="Zakupy budowlane", body="silikon\nkołki", as_list="1", pinned="1")
    r = auth_client.get("/szukaj?q=silikon")
    assert "Zakupy budowlane" in r.text
    dash = auth_client.get("/").text
    assert "Zakupy budowlane (0/2)" in dash


def test_quick_add_list_note(auth_client, db):
    r = auth_client.post(
        "/notatki", data={"body": "x\ny", "as_list": "1"}, headers={"HX-Request": "true", "X-Quick-Add": "1"}
    )
    assert r.status_code == 200 and "dm-added" in r.headers["HX-Trigger"]
    assert db.scalar(select(BoardNote)).items == [{"t": "x", "d": False}, {"t": "y", "d": False}]


def test_migration_0017(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn, conn.begin():
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(board_note)"))}
        assert {"title", "items", "color", "label"} <= cols
        command.downgrade(cfg, "7d2f9b3a1c48")
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(board_note)"))}
        assert not cols & {"title", "items", "color", "label"}
        command.upgrade(cfg, "head")


def test_edit_text_marks_open_points_and_round_trips():
    from domownik.web import items_text

    items = [{"t": "Mleko", "d": False}, {"t": "Chleb", "d": True}, {"t": "☐ w nazwie", "d": False}]
    text = items_text(items)
    assert text.splitlines()[:2] == ["☐ Mleko", "✓ Chleb"]
    assert parse_items(text) == items
    assert parse_items("[ ] jajka\n☐masło") == [{"t": "jajka", "d": False}, {"t": "masło", "d": False}]


def test_edit_form_shows_list_and_list_to_text_drops_open_marks(auth_client, db):
    _add(auth_client, title="Książki", body="Lalka\nPotop", as_list="1")
    n = db.scalar(select(BoardNote))
    page = auth_client.get("/notatki").text
    assert "☐ Lalka\n☐ Potop" in page and "note-list-hint" in page
    # the switch sits above the text in the edit form
    form = page[page.index(f'action="/notatki/{n.id}/edit"') :]
    assert form.index('name="as_list"') < form.index('name="body"')
    r = auth_client.post(
        f"/notatki/{n.id}/edit",
        data={"title": "Książki", "body": "☐ Lalka\n✓ Potop"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    db.refresh(n)
    assert not n.is_list and n.body == "Lalka\n✓ Potop"


def test_notes_board_cards_have_a_foldable_head(auth_client, db):
    _add(auth_client, title="Kod do furtki", body="1234")
    _add(auth_client, body="Bez tytułu – pierwsza linia\ndruga")
    page = auth_client.get("/notatki").text
    assert "data-notes-board" in page and page.count("data-note-head") == 2 and "data-notes-all" in page
    assert '<span class="np-text">Bez tytułu – pierwsza linia</span>' in page


def test_note_without_colour_takes_one_per_label(auth_client, db):
    _add(auth_client, title="Lampy", label="Dom")
    _add(auth_client, title="Rower", label="Hobby")
    _add(auth_client, title="Gry", label="Hobby")
    _add(auth_client, title="Wi-Fi", label="Dom", color="yellow")
    _add(auth_client, title="Paczka")
    dom, rower, gry, wifi, paczka = db.scalars(select(BoardNote).order_by(BoardNote.id)).all()
    assert dom.shade and rower.shade and dom.shade != rower.shade  # a colour per label
    assert gry.shade == rower.shade
    assert (wifi.color, wifi.shade) == ("yellow", "yellow")  # a chosen colour wins, yellow too
    assert paczka.shade is None  # no label: the plain yellow card
    page = auth_client.get("/notatki").text
    assert f"c-{dom.shade}" in page and "▾</span> Rozwiń wszystkie" in page
    assert f'class="note-chip c-{dom.shade}"' in page and f'class="note-chip c-{rower.shade}"' in page  # chips match the cards


def test_active_note_can_be_deleted(auth_client, db):
    _add(auth_client, title="Do wyrzucenia")
    n = db.scalar(select(BoardNote))
    assert f'action="/notatki/{n.id}/delete"' in auth_client.get("/notatki").text  # not only in the archive
    r = auth_client.post(f"/notatki/{n.id}/delete", follow_redirects=False)
    assert r.status_code in (303, 200)
    db.expire_all()
    assert db.scalar(select(BoardNote)) is None


def test_whole_category_gets_a_colour(auth_client, db):
    _add(auth_client, title="Plan FBW", label="Fit")
    _add(auth_client, title="Kawa", label="Fit", color="blue")  # its own colour stays
    _add(auth_client, title="Lampy", label="Dom")
    page = auth_client.get("/notatki?k=Fit").text
    assert "Kolor kategorii „Fit”" in page and "↺ automatyczny" not in page

    r = auth_client.post("/notatki/kategoria-kolor", data={"label": "Fit", "color": "pink"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/notatki?k=Fit"
    db.expire_all()
    plan, kawa, lampy = db.scalars(select(BoardNote).order_by(BoardNote.id)).all()
    db.info.pop("note_label_shades", None)
    assert (plan.shade, kawa.shade) == ("pink", "blue")
    assert lampy.shade != "pink"  # Dom keeps its automatic colour
    page = auth_client.get("/notatki?k=Fit").text
    assert 'class="note-chip c-pink"' in page and "↺ automatyczny" in page

    auth_client.post("/notatki/kategoria-kolor", data={"label": "Fit", "color": ""})  # back to automatic
    db.info.pop("note_label_shades", None)
    db.expire_all()
    assert db.get(BoardNote, plan.id).shade != "pink"


def test_migration_0033_up_and_down(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn:
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            conn.execute(sa.text("INSERT INTO note_label_color (label, color) VALUES ('Fit', 'pink')"))
            command.downgrade(cfg, "4d8a2e6f1c35")
            assert "note_label_color" not in sa.inspect(conn).get_table_names()
            command.upgrade(cfg, "head")
