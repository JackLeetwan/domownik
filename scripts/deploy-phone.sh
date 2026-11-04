#!/usr/bin/env bash
# Deploy the pushed master branch to the phone server (Termux, over adb/USB).
#   1. checks that local master is pushed (the phone pulls from GitHub),
#   2. backup on the phone (migrations run on start),
#   3. git pull; dependencies again when pyproject.toml changed; deploy/termux/setup.sh when it changed,
#   4. restart and wait for /healthz.
#   ANDROID_SERIAL – pick the phone when several devices are connected
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

P=/data/data/com.termux/files/usr
H=/data/data/com.termux/files/home
# Commands run as the Termux user (debuggable build: run-as). No login shell – it would start a second
# service daemon outside the app; services themselves are only restarted through runsv.
phone() {
  local cmd
  cmd=$(printf '%q' "set -euo pipefail; $1")
  adb shell "run-as com.termux $P/bin/env -i HOME=$H PREFIX=$P PATH=$H/bin:$P/bin TMPDIR=$P/tmp \
    SVDIR=$P/var/service LOGDIR=$P/var/log LANG=en_US.UTF-8 $P/bin/bash -c $cmd"
}

[[ "$(adb get-state 2>/dev/null)" == "device" ]] || { echo "Telefon nie jest podłączony (adb)" >&2; exit 1; }

git fetch -q origin
local_head=$(git rev-parse master) remote_head=$(git rev-parse origin/master)
if [[ "$local_head" != "$remote_head" ]]; then
  echo "master ($local_head) różni się od origin/master ($remote_head) – najpierw git push" >&2
  exit 1
fi

before=$(phone 'git -C ~/domownik rev-parse HEAD' | tr -d '\r')
if [[ "$before" == "$remote_head" ]]; then
  echo "Telefon ma już $(git log -1 --format='%h %s' "$remote_head")"
  exit 0
fi
echo "Telefon: $(git log -1 --format='%h %s' "$before" 2>/dev/null || echo "$before")"
echo "Wgrywam: $(git log -1 --format='%h %s' "$remote_head")"

phone '$HOME/bin/domownik-backup' | tail -1
phone 'cd ~/domownik && git pull -q --ff-only'
changed=$(git diff --name-only "$before" "$remote_head")
if grep -qx 'pyproject.toml' <<<"$changed"; then
  echo "Zmienione zależności – instaluję (budowanie na telefonie może potrwać kilkanaście minut)…"
  phone 'cd ~/domownik && deploy/termux/install-deps.sh'
fi
if grep -q '^deploy/termux/' <<<"$changed"; then
  phone 'cd ~/domownik && deploy/termux/setup.sh'
fi

phone 'sv restart domownik >/dev/null'
for _ in $(seq 1 60); do
  if phone 'curl -sf -m 3 http://127.0.0.1:8000/healthz >/dev/null' 2>/dev/null; then
    echo "Gotowe – działa $(git log -1 --format=%h "$remote_head")"
    exit 0
  fi
  sleep 2
done
echo "Domownik nie odpowiada po restarcie – log: \$LOGDIR/sv/domownik/current" >&2
phone 'tail -20 $LOGDIR/sv/domownik/current' >&2 || true
exit 1
