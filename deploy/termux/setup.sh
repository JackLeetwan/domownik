#!/data/data/com.termux/files/usr/bin/bash
# Put runit services, the Termux:Boot script, the backup job and cron in place (idempotent).
# Run on the phone after changes in deploy/termux/ (scripts/deploy-phone.sh does it).
set -euo pipefail
here=$(dirname "$(readlink -f "$0")")
sv_dir=$PREFIX/var/service

for p in termux-services cronie util-linux; do  # util-linux: taskset (DOMOWNIK_CPUS)
  dpkg -s "$p" >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get -y -q install "$p"
done
for name in tailscaled domownik; do
  mkdir -p "$sv_dir/$name/log"
  install -m 700 "$here/service/$name/run" "$sv_dir/$name/run"
  install -m 700 "$here/service/log-run" "$sv_dir/$name/log/run"
done
rm -f "$sv_dir/crond/down"                                    # cron on
for name in sshd ssh-agent; do [[ -d "$sv_dir/$name" ]] && touch "$sv_dir/$name/down"; done

mkdir -p "$HOME/.termux/boot" "$HOME/bin" "$HOME/log"
install -m 700 "$here/start-domownik.boot" "$HOME/.termux/boot/start-domownik"
install -m 700 "$here/domownik-backup" "$HOME/bin/domownik-backup"
crontab "$here/crontab"
[[ -f "$HOME/domownik.env" ]] || { install -m 600 "$here/domownik.env.example" "$HOME/domownik.env"; echo "Uzupełnij ~/domownik.env"; }
echo "setup OK"
