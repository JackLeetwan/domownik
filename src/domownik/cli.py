from __future__ import annotations

import getpass
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from domownik.config import get_settings, today

app = typer.Typer(help="Domownik – rodzinny panel terminów i budżetu", no_args_is_help=True)

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    return cfg


@app.command()
def migrate() -> None:
    """Apply database migrations (run on every start)."""
    command.upgrade(alembic_config(), "head")


@app.command()
def makemigration(message: str) -> None:
    """[dev] Autogenerate a migration from model changes."""
    command.revision(alembic_config(), message=message, autogenerate=True)


@app.command("create-user")
def create_user(
    email: str = typer.Option(..., prompt=True),
    name: str = typer.Option(..., prompt=True),
    password: str | None = typer.Option(None, help="Hasło (domyślnie pytanie interaktywne)"),
) -> None:
    """Create a household member account (or reset the password of an existing one)."""
    from domownik.db import session_factory
    from domownik.models import User
    from domownik.security import hash_password

    if password is None:
        password = getpass.getpass("Hasło: ")
        if password != getpass.getpass("Powtórz hasło: "):
            raise typer.BadParameter("Hasła się różnią")
    if len(password) < 8:
        raise typer.BadParameter("Hasło musi mieć co najmniej 8 znaków")
    with session_factory()() as db:
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if user:
            user.password_hash = hash_password(password)
            user.name = name
            typer.echo(f"Zaktualizowano konto {email}")
        else:
            db.add(User(email=email.strip().lower(), name=name, password_hash=hash_password(password)))
            typer.echo(f"Utworzono konto {email}")
        db.commit()


@app.command("seed-zones")
def seed_zones() -> None:
    """Create the default zones (only on an empty database)."""
    from domownik import services
    from domownik.db import session_factory
    from domownik.models import Item

    with session_factory()() as db:
        if db.scalar(select(Item.id).limit(1)):
            typer.echo("Strefy już istnieją – pomijam.")
            return
        created = services.seed_default_zones(db, today())
        typer.echo("Utworzono: " + ", ".join(i.name for i in created))


@app.command()
def export(
    output: Path = typer.Option(None, help="Plik .tar.gz (domyślnie data/backups/domownik-<data>.tar.gz)"),
) -> None:
    """Pack the database and uploads into one archive (for backups and moving to another machine)."""
    s = get_settings()
    output = output or s.backups_dir / f"domownik-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    _write_archive(output, include_secrets=True)
    typer.echo(f"Zapisano {output}")


@app.command()
def backup(keep: int = typer.Option(14, help="Ile ostatnich kopii zostawić")) -> None:
    """Daily backup: archive without secrets in data/backups/auto, keep the newest N."""
    s = get_settings()
    auto = s.backups_dir / "auto"
    auto.mkdir(parents=True, exist_ok=True)
    output = auto / f"domownik-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    # no session secret: the copy may leave this machine (cloud drive); a restore just logs everyone out
    _write_archive(output, include_secrets=False)
    for old in sorted(auto.glob("domownik-*.tar.gz"))[: -max(keep, 1)]:
        old.unlink()
    typer.echo(str(output))


def _write_archive(output: Path, *, include_secrets: bool) -> None:
    s = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "db.sqlite"
        src = sqlite3.connect(s.db_path)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with tarfile.open(output, "w:gz") as tar:
            tar.add(snap, arcname="db.sqlite")
            if s.uploads_dir.exists():
                tar.add(s.uploads_dir, arcname="uploads")
            if include_secrets and s.secrets_dir.exists():
                tar.add(s.secrets_dir, arcname="secrets")


@app.command("import")
def import_(archive: Path, force: bool = typer.Option(False, help="Nadpisz istniejącą bazę")) -> None:
    """Restore an archive created by `export` into the data directory."""
    s = get_settings()
    if s.db_path.exists() and not force:
        raise typer.BadParameter(f"{s.db_path} istnieje – użyj --force, żeby nadpisać")
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(s.data_dir, filter="data")
    for suffix in ("-wal", "-shm"):
        Path(f"{s.db_path}{suffix}").unlink(missing_ok=True)
    typer.echo(f"Przywrócono dane do {s.data_dir}")
    migrate()


@app.command()
def notify(
    slot: str = typer.Argument("morning", help="morning albo evening"),
    send: bool = typer.Option(False, help="Wyślij naprawdę (z zapisem w logu), zamiast tylko pokazać"),
) -> None:
    """Show (or send) the digest each user would get now."""
    from domownik import notify as nt
    from domownik.db import session_factory

    if slot not in ("morning", "evening"):
        raise typer.BadParameter("slot: morning albo evening")
    with session_factory()() as db:
        if send:
            typer.echo(f"Wysłano: {nt.run_slot(db, slot, today())}")
            return
        digests = nt.build_digests(db, slot, today())
        if not digests:
            typer.echo("Nikt nie ma ustawionego tematu ntfy (albo ma wyłączony ten skrót).")
        for d in digests:
            typer.echo(f"── {d.user.name}: {d.title()}")
            typer.echo(d.message() or "(nic nowego – nie wyśle się)")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Run migrations and start the web server."""
    import uvicorn

    migrate()
    uvicorn.run("domownik.main:app", host=host, port=port, reload=reload, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    app()
