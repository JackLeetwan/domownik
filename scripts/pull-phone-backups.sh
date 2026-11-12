#!/usr/bin/env bash
# Copy new backup archives from the phone server (Termux, over adb/USB) to this machine.
# The phone makes the archives itself (cron: `domownik backup`); this only fetches them.
# Does nothing when no phone is connected, so it is safe to run from a frequent timer.
# Keeps at most one archive per day (the first one of each day newer than the newest
# already copied): the phone makes one before every deploy, and each rotated-out archive
# sits in the Drive trash for 30 days, still counting against the quota.
#   DOMOWNIK_BACKUP_DEST        – target dir (default ~/GoogleDrive/Domownik/kopie)
#   DOMOWNIK_BACKUP_KEEP        – archives (= days) kept in the target dir (default 14)
#   DOMOWNIK_PHONE_BACKUPS      – backup dir on the phone
#   ANDROID_SERIAL              – pick the phone when several devices are connected
set -euo pipefail

dest="${DOMOWNIK_BACKUP_DEST:-$HOME/GoogleDrive/Domownik/kopie}"
keep="${DOMOWNIK_BACKUP_KEEP:-14}"
remote="${DOMOWNIK_PHONE_BACKUPS:-/data/data/com.termux/files/home/domownik-data/backups/auto}"

command -v adb >/dev/null || exit 0
[[ "$(adb get-state 2>/dev/null)" == "device" ]] || exit 0

# never write into an unmounted mount point (files would land on the local disk)
if [[ "$dest" == "$HOME/GoogleDrive"* ]] && ! mountpoint -q "$HOME/GoogleDrive"; then
  echo "Dysk Google nie jest zamontowany – pomijam" >&2
  exit 1
fi
mkdir -p "$dest"

# day (YYYYMMDD) of the newest archive already copied; only later days are fetched,
# so archives rotated out here are never fetched again while the phone still has them
last_day=$(ls -1 "$dest" 2>/dev/null | sed -nE 's/^domownik-([0-9]{8})-[0-9]{6}\.tar\.gz$/\1/p' | sort | tail -n1)

new=0
while read -r name; do
  [[ "$name" =~ ^domownik-([0-9]{8})-[0-9]{6}\.tar\.gz$ ]] || continue
  day="${BASH_REMATCH[1]}"
  [[ "$day" > "$last_day" ]] || continue
  tmp="$dest/.$name.part"
  adb exec-out run-as com.termux cat "$remote/$name" > "$tmp"
  if gzip -t "$tmp" 2>/dev/null; then
    mv "$tmp" "$dest/$name"
    echo "Pobrano $name"
    new=$((new + 1))
    last_day="$day"
  else
    rm -f "$tmp"
    echo "Uszkodzona kopia $name – pomijam" >&2
  fi
done < <(adb shell run-as com.termux ls -1 "$remote" 2>/dev/null | tr -d '\r' | sort)

ls -1t "$dest"/domownik-*.tar.gz 2>/dev/null | tail -n +"$((keep + 1))" | xargs -r rm --
[[ $new -gt 0 ]] && echo "Nowych kopii: $new → $dest" || true
