#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot (~/.termux/boot/): keep the CPU awake and start runit (tailscaled, domownik, crond).
# Runs only after the first unlock – leave the phone without a screen lock.
termux-wake-lock
. /data/data/com.termux/files/usr/etc/profile.d/start-services.sh
