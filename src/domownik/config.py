from __future__ import annotations

import secrets
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment / .env – nothing host specific in code."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DOMOWNIK_", extra="ignore")

    data_dir: Path = Path("data")
    secret_key: str = ""
    base_url: str = "http://localhost:8000"
    timezone: str = "Europe/Warsaw"
    # Set to true when served over HTTPS (tailscale serve, Caddy) – marks the session cookie Secure
    https_only: bool = False
    session_days: int = 90
    # push notifications (Web Push) – each user enables them on their phone (Ustawienia)
    morning_hour: int = 8
    evening_hour: int = 19
    # background jobs (digests, autopay); off in tests and throwaway dev copies
    scheduler: bool = True
    # requests at least this slow go to the log ("SLOW …", domownik.slowlog); 0 = off
    slow_ms: int = 150
    # documents and photos kept with items (data/uploads/files)
    max_upload_mb: int = 20
    max_upload_files: int = 20

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db.sqlite"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def files_dir(self) -> Path:
        return self.uploads_dir / "files"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def resolved_secret_key(self) -> str:
        """Use DOMOWNIK_SECRET_KEY, otherwise a key generated once and kept in data/secrets."""
        if self.secret_key:
            return self.secret_key
        path = self.secrets_dir / "secret_key"
        if path.exists():
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        path.write_text(key)
        path.chmod(0o600)
        return key


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.uploads_dir, s.secrets_dir, s.backups_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s


def now() -> datetime:
    return datetime.now(get_settings().tz)


def today() -> date:
    return now().date()
