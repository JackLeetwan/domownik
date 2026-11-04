#!/data/data/com.termux/files/usr/bin/bash
# Build tailscaled + tailscale for Android in Termux (run on the phone).
# The official linux/arm64 binary dies inside the Termux app (seccomp: SIGSYS on faccessat2);
# a GOOS=android build avoids it. SSH and Taildrop expect the official Android app – left out.
#   build-tailscaled.sh 1.104.5      (needs the Go version from that release's go.mod: pkg golang)
set -euo pipefail
ver=${1:?podaj wersję, np. 1.104.5}
dpkg -s golang >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get -y -q install golang
export GOTOOLCHAIN=local GOFLAGS=-trimpath
go install -tags ts_omit_ssh,ts_omit_taildrop "tailscale.com/cmd/tailscaled@v$ver"
go install -tags ts_omit_ssh,ts_omit_taildrop "tailscale.com/cmd/tailscale@v$ver"
mkdir -p "$HOME/bin"
install -m 755 "$HOME/go/bin/tailscaled" "$HOME/go/bin/tailscale" "$HOME/bin/"
"$HOME/bin/tailscale" version | head -1
echo "Restart: sv restart tailscaled"
