#!/usr/bin/env bash
# Wildlife Network Base installer. Run as root on a fresh Raspberry Pi OS Lite (64-bit, Bookworm or later):
#   sudo ./install.sh
# Safe to run again: existing configs, the setup code and an existing BirdNET-Go install are kept.
# Options (environment variables):
#   WN_USER              user that runs BirdNET-Go and wdx-agent (default: the user who ran sudo, else uid 1000)
#   WN_BASE_URL          Wildlife Network site (default https://wlnw.org)
#   WN_SKIP_BIRDNET=1    do not install BirdNET-Go
#   WN_BIRDNET_VERSION   BirdNET-Go release (default: the one pinned in image/VERSIONS, as in the SD card image;
#                        "latest" takes BirdNET-Go's newest, which wdx-agent may not read yet)
#   WN_WIFI_COUNTRY      two-letter Wi-Fi country code, needed once for the hotspot (e.g. GB, US, IN)
#   WN_INSTALL_LOG       install log (default /var/log/wnbase-install.log, appended on every run)
set -euo pipefail

say() { printf '\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${WN_BASE_URL:-https://wlnw.org}"
RUN_USER="${WN_USER:-${SUDO_USER:-}}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = root ]; then
  RUN_USER="$(getent passwd 1000 | cut -d: -f1 || true)"
fi
[ -n "$RUN_USER" ] || { echo "No regular user found; set WN_USER"; exit 1; }
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

# Everything below also goes to the install log, readable by $RUN_USER (no sudo needed to send it for help).
LOG="${WN_INSTALL_LOG:-/var/log/wnbase-install.log}"
touch "$LOG"
chown "root:$(id -gn "$RUN_USER")" "$LOG"
chmod 640 "$LOG"
exec > >(tee -a "$LOG") 2>&1
echo "=== install.sh $(date -u +%Y-%m-%dT%H:%M:%SZ) ($(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo 'no git'))"
trap 'echo "Install log: $LOG"' EXIT
say "Installing the Wildlife Network Base software for user $RUN_USER"

if [ -n "${WN_WIFI_COUNTRY:-}" ] && command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country "$WN_WIFI_COUNTRY"
fi

# --- packages, wdx-agent, wnbase, configs and services (shared with the SD card image build) -----------------
WN_USER="$RUN_USER" WN_BASE_URL="$BASE_URL" WN_SRC="$HERE" bash "$HERE/base/provision.sh" --live

# --- BirdNET-Go (official installer, Docker based; data in ~/birdnet-go-app) ----------------------------------
BNG_CFG="$RUN_HOME/birdnet-go-app/config/config.yaml"
# Same BirdNET-Go release as the SD card image: its database layout is the one wdx-agent is tested against.
BNG_PINNED="$(sed -n 's/^BIRDNET_GO_IMAGE=.*:\([^:]*\)$/\1/p' "$HERE/image/VERSIONS" 2>/dev/null || true)"
BNG_VERSION="${WN_BIRDNET_VERSION:-${BNG_PINNED:-latest}}"
# The installer from that release's tag (it matches the release); main's installer for "latest".
BNG_REF="$BNG_VERSION"; [ "$BNG_VERSION" = latest ] && BNG_REF=main
RERUN="cd '$HERE' && sudo ./install.sh"
fail() {
  printf '\033[1;31m%s\033[0m\n' "$@" >&2
  exit 1
}
if [ "${WN_SKIP_BIRDNET:-0}" = 1 ]; then
  warn "Skipping BirdNET-Go (WN_SKIP_BIRDNET=1)"
elif [ -f /etc/systemd/system/birdnet-go.service ] || [ -f /lib/systemd/system/birdnet-go.service ] \
     || [ -f "$BNG_CFG" ]; then
  say "BirdNET-Go is already installed"
else
  say "Installing BirdNET-Go $BNG_VERSION with its official installer (this takes a while)"
  # The installer refuses to run as root and calls sudo itself. Without a terminal those sudo calls cannot ask for
  # a password and the installer can stop early, so make sure sudo works for $RUN_USER before starting.
  if ! sudo -u "$RUN_USER" -H sudo -n true 2>/dev/null; then
    if [ -t 0 ]; then
      say "BirdNET-Go's installer needs sudo as $RUN_USER. Enter the password of $RUN_USER if asked."
      sudo -u "$RUN_USER" -H sudo -v || fail "sudo did not work for $RUN_USER; BirdNET-Go was not installed."
    else
      fail "BirdNET-Go's installer needs sudo as $RUN_USER, which asks for a password, and there is no terminal to type it." \
           "Run this again from an SSH session with a terminal (ssh -t ...):" "  $RERUN"
    fi
  fi
  bng="$(mktemp -d)"
  curl -fsSL "https://github.com/tphakala/birdnet-go/raw/$BNG_REF/install.sh" -o "$bng/install.sh"
  chown -R "$RUN_USER" "$bng"
  # --silent takes settings from BIRDNET_* variables. The location is written into its config.yaml later, when it
  # is entered in the Base setup.
  rc=0
  (cd "$bng" && sudo -u "$RUN_USER" -H env BIRDNET_TELEMETRY=false bash ./install.sh -v "$BNG_VERSION" --silent) || rc=$?
  rm -rf "$bng"
  # Its exit status is not enough: it has exited 0 after creating only the log folder.
  if ! out="$(bash "$HERE/base/birdnet-go-check.sh" "$BNG_CFG" 90)"; then
    fail "$out" "BirdNET-Go's installer stopped early (exit status $rc). Nothing else is wrong; run this again" \
         "from an SSH session with a terminal:" "  $RERUN"
  fi
  say "$out"
fi

# --- microphone: BirdNET-Go's default "sysdefault" is the Pi's onboard audio, which cannot record ------------
if [ "${WN_SKIP_BIRDNET:-0}" != 1 ] && [ -f "$BNG_CFG" ]; then
  # Also on every BirdNET-Go start (a microphone plugged in later, or a new card number after a reboot). The SD card
  # image has this line in its own unit; the official installer's unit gets it as a drop-in.
  if [ -f /etc/systemd/system/birdnet-go.service ] && ! grep -q mic.py /etc/systemd/system/birdnet-go.service; then
    install -d -m 755 /etc/systemd/system/birdnet-go.service.d
    printf '[Service]\nExecStartPre=-/usr/bin/python3 /opt/wnbase/mic.py --config %s --state /var/lib/wnbase/mic-device\n' \
      "$BNG_CFG" > /etc/systemd/system/birdnet-go.service.d/wnbase-mic.conf
    systemctl daemon-reload
  fi
  python3 /opt/wnbase/mic.py --list
  report() { while IFS= read -r line; do case "$line" in WARNING:*) warn "$line" ;; *) say "$line" ;; esac; done <<<"$1"; }
  mic_out="$(python3 /opt/wnbase/mic.py --config "$BNG_CFG" --state /var/lib/wnbase/mic-device)"; report "$mic_out"
  # BirdNET-Go's settings page is open to anyone on the Wi-Fi: put it behind a password of this Base.
  auth_out="$(python3 /opt/wnbase/bngauth.py --config "$BNG_CFG")"; report "$auth_out"
  if grep -q "microphone set to\|now need a password" <<<"$mic_out$auth_out"; then
    if systemctl restart birdnet-go.service 2>/dev/null || docker restart birdnet-go >/dev/null; then say "BirdNET-Go restarted"
    else warn "Could not restart BirdNET-Go; restart it yourself (sudo systemctl restart birdnet-go)"; fi
  fi
fi

CODE="$(python3 /opt/wnbase/wnbase.py --config /etc/wnbase/wnbase.ini --setup-code)"
SSID="$(python3 /opt/wnbase/wnbase.py --config /etc/wnbase/wnbase.ini --hotspot-env | cut -f2)"

if command -v rfkill >/dev/null && rfkill list wifi 2>/dev/null | grep -q "Soft blocked: yes"; then
  warn "Wi-Fi is blocked until a country is set: run again with WN_WIFI_COUNTRY=XX (two-letter code)"
fi

# Same as the SD card image: the details on the boot partition, for a Base whose label is lost.
bash "$HERE/base/setup-file.sh" "$(hostname)" "$SSID" "$CODE"

say "Done."
echo
echo "  Hotspot (Wi-Fi):  $SSID"
echo "  Password:         wn-$CODE"
echo "  Setup code:       $CODE"
echo "  Then open:        http://10.42.0.1"
echo
echo "Write these on the label of the Base. On a network the dashboard is also at http://$(hostname).local"
echo "The hotspot starts only when home Wi-Fi is not available (or run: sudo /opt/wnbase/hotspot.sh, which drops a Wi-Fi SSH session)."
