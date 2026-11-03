from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event, or_
from sqlalchemy.engine import Engine
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker, with_loader_criteria

from domownik.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
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


def get_db() -> Iterator[Session]:
    with session_factory()() as session:
        yield session


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
    from domownik.models import BoardNote, Task, Todo

    vid = state.session.info[VIEWER] or 0
    state.statement = state.statement.options(
        *(
            with_loader_criteria(
                model, lambda cls: or_(cls.private_to_id.is_(None), cls.private_to_id == vid), include_aliases=True
            )
            for model in (Task, BoardNote, Todo)
        )
    )
