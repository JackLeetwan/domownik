from __future__ import annotations

from alembic import context

from domownik.db import get_engine
from domownik.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection")
    if connectable is not None:
        _run(connectable, own=False)  # caller's connection and transaction – leave pragmas to the caller
        return
    with get_engine().connect() as connection:
        _run(connection, own=True)


def _run(connection, *, own: bool) -> None:
    # SQLite batch migrations rebuild a table (create new, copy, DROP old, rename). With foreign keys ON
    # the DROP fires ON DELETE CASCADE / SET NULL in every referencing table – tasks vanish, "who did it"
    # columns are wiped. So migrations run with foreign keys OFF and are checked afterwards instead.
    sqlite = own and connection.dialect.name == "sqlite"
    if sqlite:
        if connection.in_transaction():
            connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
    try:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
            if sqlite:
                broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise RuntimeError(f"Migration left broken foreign keys: {broken[:5]}")
    finally:
        if sqlite:
            if connection.in_transaction():
                connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


run_migrations_online()
