# Domownik

Rodzinny panel: strefy (Samochody, Dom, Mieszkanie wynajęte, Dzieci, Okazje…), zadania cykliczne,
oś czasu, historia z kosztami, a w kolejnych etapach budżet i powiadomienia.

Plan: `~/.claude/plans/glowing-beaming-catmull.md`.

## Uruchomienie (Docker)

```bash
cp .env.example .env        # na tym PC: DOMOWNIK_UID=0 / DOMOWNIK_GID=0 (userns-remap)
docker compose up -d --build
docker compose exec app domownik create-user     # konto dla każdego domownika
```

Aplikacja słucha na `127.0.0.1:8000`. Dostęp z telefonów przez Tailscale:

```bash
sudo tailscale serve --bg 8000      # → https://<host>.<tailnet>.ts.net
```

Po włączeniu HTTPS ustaw w `.env`: `DOMOWNIK_HTTPS_ONLY=true` i `DOMOWNIK_BASE_URL=https://…`,
potem `docker compose up -d`.

## Rozwój

```bash
uv sync
uv run pytest
DOMOWNIK_DATA_DIR=/tmp/dm uv run domownik serve --reload --port 8765
uv run domownik makemigration "opis zmiany"     # po zmianie models.py
```

## Komendy

| Komenda | Co robi |
|---|---|
| `domownik serve` | migracje + serwer |
| `domownik create-user` | nowe konto / reset hasła |
| `domownik seed-zones` | domyślne strefy (pusta baza) |
| `domownik export` / `import` | archiwum bazy i plików (backup, przeprowadzka) |

## Struktura

- `src/domownik/domain/recurrence.py` – silnik terminów (czyste funkcje, testy w `tests/test_recurrence.py`)
- `src/domownik/services.py` – logika aplikacji (szablony → zadania, „Zrobione”, oś czasu, statystyki stref)
- `src/domownik/catalog/templates/*.yaml` – szablony elementów z proponowanymi zadaniami
- `src/domownik/catalog/seed/*.yaml` – domyślne strefy i kategorie budżetu
- `data/` – cały stan (SQLite, załączniki, sekrety, kopie) – poza gitem
