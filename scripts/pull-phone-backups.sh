#!/usr/bin/env bash
# Copy new backup archives from the phone server (Termux, over adb/USB) to this machine.
# The phone makes the archives itself (cron: `domownik backup`); this only fetches them.
# Does nothing when no phone is connected, so it is safe to run from a frequent timer.
#   DOMOWNIK_BACKUP_DEST        – target dir (default ~/GoogleDrive/Domownik/kopie)
#   DOMOWNIK_BACKUP_KEEP        – archives kept in the target dir (default 30)
#   DOMOWNIK_PHONE_BACKUPS      – backup dir on the phone
#   ANDROID_SERIAL              – pick the phone when several devices are connected
set -euo pipefail

dest="${DOMOWNIK_BACKUP_DEST:-$HOME/GoogleDrive/Domownik/kopie}"
keep="${DOMOWNIK_BACKUP_KEEP:-30}"
remote="${DOMOWNIK_PHONE_BACKUPS:-/data/data/com.termux/files/home/domownik-data/backups/auto}"

command -v adb >/dev/null || exit 0
[[ "$(adb get-state 2>/dev/null)" == "device" ]] || exit 0

# never write into an unmounted mount point (files would land on the local disk)
if [[ "$dest" == "$HOME/GoogleDrive"* ]] && ! mountpoint -q "$HOME/GoogleDrive"; then
  echo "Dysk Google nie jest zamontowany – pomijam" >&2
  exit 1
fi
mkdir -p "$dest"

new=0
while read -r name; do
  [[ "$name" =~ ^domownik-[0-9]{8}-[0-9]{6}\.tar\.gz$ ]] || continue
  [[ -e "$dest/$name" ]] && continue
  tmp="$dest/.$name.part"
  adb exec-out run-as com.termux cat "$remote/$name" > "$tmp"
  if gzip -t "$tmp" 2>/dev/null; then
    mv "$tmp" "$dest/$name"
    echo "Pobrano $name"
    new=$((new + 1))
  else
    rm -f "$tmp"
    echo "Uszkodzona kopia $name – pomijam" >&2
  fi
done < <(adb shell run-as com.termux ls -1 "$remote" 2>/dev/null | tr -d '\r')

ls -1t "$dest"/domownik-*.tar.gz 2>/dev/null | tail -n +"$((keep + 1))" | xargs -r rm --
[[ $new -gt 0 ]] && echo "Nowych kopii: $new → $dest" || true
