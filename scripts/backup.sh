#!/usr/bin/env bash
# Daily backup: archive inside the container (data/backups/auto, rotated there),
# then copy the newest archive off this disk (default: Google Drive mount).
#   DOMOWNIK_BACKUP_DEST – target dir (default ~/GoogleDrive/Domownik/kopie)
#   DOMOWNIK_BACKUP_KEEP – archives kept in the target dir (default 30)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

dest="${DOMOWNIK_BACKUP_DEST:-$HOME/GoogleDrive/Domownik/kopie}"
keep="${DOMOWNIK_BACKUP_KEEP:-30}"

docker compose exec -T app domownik backup --keep 14 >/dev/null
latest=$(ls -1t data/backups/auto/domownik-*.tar.gz | head -n1)
echo "Kopia lokalna: $latest"

# never write into an unmounted mount point (files would land on the local disk)
if [[ "$dest" == "$HOME/GoogleDrive"* ]] && ! mountpoint -q "$HOME/GoogleDrive"; then
  echo "Dysk Google nie jest zamontowany – kopia została tylko lokalnie" >&2
  exit 1
fi
mkdir -p "$dest"
cp "$latest" "$dest/"
ls -1t "$dest"/domownik-*.tar.gz | tail -n +"$((keep + 1))" | xargs -r rm --
echo "Skopiowano do $dest"
