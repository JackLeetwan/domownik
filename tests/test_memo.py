"""Per-session memo (db.memoized): repeats within one request are free, but nothing written is ever missed."""

from __future__ import annotations

from datetime import date

from sqlalchemy import update

from domownik import services
from domownik.db import memoized, set_viewer
from domownik.domain.tree import Tree
from domownik.models import BoardNote, Item, Todo

TODAY = date(2026, 11, 4)


def test_repeat_is_memoized(db):
    db.add(Item(name="Dom", kind="zone"))
    db.commit()
    first = Tree.load(db)
    assert Tree.load(db) is first
    calls = []
    memoized(db, ("k",), lambda: calls.append(1) or 1)
    memoized(db, ("k",), lambda: calls.append(1) or 1)
    assert calls == [1]


def test_commit_forgets(db):
    db.add(Item(name="Dom", kind="zone"))
    db.commit()
    assert [i.name for i in Tree.load(db).roots()] == ["Dom"]
    db.add(Item(name="Auto", kind="zone"))
    db.commit()
    assert sorted(i.name for i in Tree.load(db).roots()) == ["Auto", "Dom"]


def test_pending_changes_are_seen_like_a_fresh_query(db):
    db.add(Item(name="Dom", kind="zone"))
    db.commit()
    Tree.load(db)
    db.add(Item(name="Auto", kind="zone"))  # not flushed – a query would autoflush it
    assert sorted(i.name for i in Tree.load(db).roots()) == ["Auto", "Dom"]


def test_bulk_update_forgets(db):
    item = Item(name="Dom", kind="zone")
    db.add(item)
    db.commit()
    db.add(Todo(title="Wymienić żarówkę", item_id=item.id))
    db.commit()
    tree = Tree.load(db)
    assert len(services.open_todos(db, tree)) == 1
    db.execute(update(Todo).values(done_on=TODAY))
    assert services.open_todos(db, Tree.load(db)) == []


def test_rollback_forgets(db):
    db.add(BoardNote(body="Kupić mleko"))
    db.commit()
    assert len(services.active_notes(db, TODAY)) == 1
    db.add(BoardNote(body="Zadzwonić"))
    db.flush()
    assert len(services.active_notes(db, TODAY)) == 2
    db.rollback()
    assert len(services.active_notes(db, TODAY)) == 1


def test_viewer_is_part_of_the_key(db, user):
    db.add(BoardNote(body="Wspólna"))
    db.add(BoardNote(body="Tylko moja", private_to_id=user.id))
    db.commit()
    set_viewer(db, user.id)
    assert len(services.active_notes(db, TODAY)) == 2
    set_viewer(db, user.id + 1)
    assert [n.body for n in services.active_notes(db, TODAY)] == ["Wspólna"]


def test_callers_get_their_own_list(db):
    db.add(BoardNote(body="Kupić mleko"))
    db.commit()
    services.active_notes(db, TODAY).clear()
    assert len(services.active_notes(db, TODAY)) == 1
