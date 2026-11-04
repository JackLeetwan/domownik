#!/usr/bin/env bash
# Sum up the "SLOW …" lines of the phone server (domownik.slowlog) per route: how often, median, worst.
#   scripts/phone-slow-report.sh          (phone on USB; reads the current and rotated runit logs)
set -euo pipefail
log=/data/data/com.termux/files/usr/var/log/sv/domownik
[[ "$(adb get-state 2>/dev/null)" == "device" ]] || { echo "Telefon nie jest podłączony (adb)" >&2; exit 1; }
adb shell "run-as com.termux sh -c 'cat $log/@*.s $log/current 2>/dev/null'" | tr -d '\r' | python3 -c '
import re, statistics, sys
rows = {}
first = last = None
for line in sys.stdin:
    m = re.match(r"(\S+) SLOW\s+(\d+) ms\s+(\S+)\s+(\S+)\s+(\d+)\s*(\S*)", line)
    if not m:
        continue
    ts, ms, method, route, status, kind = m.groups()
    first = first or ts; last = ts
    rows.setdefault((method, route), []).append(int(ms))
if not rows:
    print("Brak wolnych żądań w logu."); sys.exit()
print(f"Od {first[:16]} do {last[:16]}\n")
print(f"{\"ile\":>5} {\"mediana\":>8} {\"najgorzej\":>10}  trasa")
for (method, route), v in sorted(rows.items(), key=lambda kv: -sum(kv[1])):
    print(f"{len(v):5d} {statistics.median(v):6.0f} ms {max(v):7d} ms  {method} {route}")
'
