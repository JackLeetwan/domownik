from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

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
