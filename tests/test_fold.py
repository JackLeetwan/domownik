"""Pages whose sections fold to their heading (app.js, data-fold-scope) – the markup it relies on."""

from __future__ import annotations

from domownik.models import Contact, Item


def test_item_page_is_one_fold_scope_with_counts(auth_client, db):
    item = Item(name="Dom", kind="house")
    db.add(item)
    db.commit()
    page = auth_client.get(f"/items/{item.id}").text
    assert f'data-fold-scope="item-{item.id}"' in page
    assert '<section id="zadania">' in page and 'id="sprawy"' in page and 'id="dziennik"' in page


def test_contacts_fold_but_search_results_do_not(auth_client, db):
    db.add_all([Contact(name="Hydraulik Jan", role="fachowiec"), Contact(name="Bank X", role="bank")])
    db.commit()
    page = auth_client.get("/kontakty").text
    assert 'data-fold-scope="kontakty"' in page and page.count('<section class="contact-group">') == 2
    found = auth_client.get("/kontakty?q=hydraulik").text
    assert "data-fold-scope" not in found and "Hydraulik Jan" in found


def test_emergency_page_folds_but_the_printout_has_no_sections(auth_client):
    page = auth_client.get("/na-wypadek").text
    assert 'data-fold-scope="na-wypadek"' in page and page.count('class="em-section"') >= 13
    printout = auth_client.get("/na-wypadek/druk").text
    assert "data-fold-scope" not in printout and "em-section" not in printout


def test_todo_groups_fold_and_start_open(auth_client):
    auth_client.post("/do-zrobienia", data={"title": "Wymienić żarówkę"})
    page = auth_client.get("/do-zrobienia").text
    assert 'data-fold-scope="do-zrobienia"' in page and 'class="todo-group-box" data-fold-default="open"' in page
