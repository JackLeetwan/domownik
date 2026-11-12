from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Request
from sqlalchemy import create_engine, event, or_
from sqlalchemy.engine import Engine
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker, with_loader_criteria

from domownik.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _lower(value):
    """SQLite's own lower() knows only ASCII: lower('Śmietana') stays 'Śmietana', so a Polish first letter
    never matched name.lower() in Python. Replaced on every connection with Python's (no index uses it)."""
    return value.lower() if isinstance(value, str) else value


def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
    dbapi_conn.create_function("lower", 1, _lower, deterministic=True)
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False})
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = make_engine(f"sqlite:///{get_settings().db_path}")
        _session_factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def set_engine(engine: Engine) -> None:
    """Used by tests to point the app at a temporary database."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(engine, expire_on_commit=False)


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def get_db(request: Request) -> Iterator[Session]:
    with session_factory()() as session:
        # the page's sidebar (web.render) reads with the same session – and the same memo below
        request.state.db = session
        try:
            yield session
        finally:
            request.state.db = None


# ---------------------------------------------------------------- per-session memo
# One page asks for the same things more than once (the dashboard and the sidebar both want the zone tree,
# the task list, notes, todos, chores). On a small server every repeat costs real time, so read-only helpers
# keep their result in the session until anything is written: every flush, commit, rollback or non-SELECT
# statement empties the memo. A session lives for one request, so nothing outlives the page.

MEMO = "memo"


def memoized[T](db: Session, key: tuple, compute: Callable[[], T]) -> T:
    if db.new or db.dirty or db.deleted:
        # unflushed changes: a fresh query would autoflush and see them – so must we
        forget(db)
        return compute()
    store = db.info.setdefault(MEMO, {})
    full = (key, db.info.get(VIEWER))  # private rows depend on who is looking
    if full not in store:
        store[full] = compute()
    return store[full]


def forget(db: Session) -> None:
    db.info.pop(MEMO, None)


@event.listens_for(Session, "after_flush")
def _forget_after_flush(session: Session, _ctx) -> None:
    forget(session)


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _forget_after_tx(session: Session) -> None:
    forget(session)


@event.listens_for(Session, "after_soft_rollback")
def _forget_after_soft_rollback(session: Session, _previous) -> None:
    forget(session)


@event.listens_for(Session, "do_orm_execute")
def _forget_after_write(state: ORMExecuteState) -> None:
    if not state.is_select:
        forget(state.session)


# ---------------------------------------------------------------- private tasks / notes / todos
# A session that knows who is looking (db.info["viewer_id"], set by security.current_user, the ICS feed and
# the menu) never loads someone else's private rows – lists, the timeline, the calendar, search, db.get().
# Sessions without a viewer (scheduler, CLI) see everything and filter themselves (notify.py).
# A statement can opt out with .execution_options(all_private=True) – the shared budget counts every amount.

VIEWER = "viewer_id"


def set_viewer(db: Session, user_id: int | None) -> None:
    db.info[VIEWER] = user_id


@event.listens_for(Session, "do_orm_execute")
def _private_filter(state: ORMExecuteState) -> None:
    if VIEWER not in state.session.info or not state.is_select or state.execution_options.get("all_private"):
        return
    if state.is_column_load or state.is_relationship_load:
        return  # the criteria below propagate to these loads by themselves
    from domownik.models import Attachment, BoardNote, ExternalCalendar, HealthLink, Issue, Medication, Task, Todo

    vid = state.session.info[VIEWER] or 0
    state.statement = state.statement.options(
        *(
            with_loader_criteria(
                model, lambda cls: or_(cls.private_to_id.is_(None), cls.private_to_id == vid), include_aliases=True
            )
            for model in (Task, BoardNote, Todo, ExternalCalendar, Issue, Attachment, Medication, HealthLink)
        )
    )
