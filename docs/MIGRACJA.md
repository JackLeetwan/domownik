# Przeprowadzka Domownika na inny sprzęt / do chmury

Cały stan to katalog `data/` + plik `.env`. Kod jest w gicie.

## Wariant A – kopiowanie katalogu (najprościej)

```bash
# stary host
docker compose down
rsync -a ~/domownik/ nowy-host:~/domownik/      # razem z data/ i .env
# nowy host
cd ~/domownik
# ustaw DOMOWNIK_UID/GID w .env: 1000 bez userns-remap, 0 przy userns-remap
docker compose up -d --build
sudo tailscale serve --bg 8000
```

## Wariant B – archiwum

```bash
# stary host
docker compose exec app domownik export          # → data/backups/domownik-<data>.tar.gz
# nowy host (świeży klon repo)
mkdir -p data && cp domownik-*.tar.gz data/
docker compose run --rm app domownik import /data/domownik-<data>.tar.gz
docker compose up -d
```

## Chmura (VPS)

- Tak jak wyżej, a potem albo Tailscale na VPS (nic publicznie), albo HTTPS przez Caddy:
  `DOMOWNIK_DOMAIN=domownik.example.com` w `.env` i `docker compose --profile cloud up -d`.
- Ustaw `DOMOWNIK_HTTPS_ONLY=true`. Logowanie, CSRF i limit prób logowania są włączone od początku.
- Obraz da się zbudować na amd64 i arm64 (Raspberry Pi, tani VPS ARM):
  `docker buildx build --platform linux/amd64,linux/arm64 .`
