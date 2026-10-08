#!/usr/bin/env bash
# WildNetwork Base installer. Run as root on a fresh Raspberry Pi OS Lite (64-bit, Bookworm or later):
#   sudo ./install.sh
# Safe to run again: existing configs, the setup code and an existing BirdNET-Go install are kept.
# Options (environment variables):
#   WN_USER              user that runs BirdNET-Go and wdx-agent (default: the user who ran sudo, else uid 1000)
#   WN_BASE_URL          WildNetwork site (default https://wildnetwork.arunrajiah.com)
#   WN_SKIP_BIRDNET=1    do not install BirdNET-Go
#   WN_BIRDNET_VERSION   BirdNET-Go image tag for its installer (default: latest)
#   WN_WIFI_COUNTRY      two-letter Wi-Fi country code, needed once for the hotspot (e.g. GB, US, IN)
set -euo pipefail

say() { printf '\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${WN_BASE_URL:-https://wildnetwork.arunrajiah.com}"
RUN_USER="${WN_USER:-${SUDO_USER:-}}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = root ]; then
  RUN_USER="$(getent passwd 1000 | cut -d: -f1 || true)"
fi
[ -n "$RUN_USER" ] || { echo "No regular user found; set WN_USER"; exit 1; }
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
say "Installing the WildNetwork Base software for user $RUN_USER"

if [ -n "${WN_WIFI_COUNTRY:-}" ] && command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country "$WN_WIFI_COUNTRY"
fi

# --- packages, wdx-agent, wnbase, configs and services (shared with the SD card image build) -----------------
WN_USER="$RUN_USER" WN_BASE_URL="$BASE_URL" WN_SRC="$HERE" bash "$HERE/base/provision.sh" --live

# --- BirdNET-Go (official installer, Docker based; data in ~/birdnet-go-app) ----------------------------------
if [ "${WN_SKIP_BIRDNET:-0}" = 1 ]; then
  warn "Skipping BirdNET-Go (WN_SKIP_BIRDNET=1)"
elif [ -f /etc/systemd/system/birdnet-go.service ] || [ -f /lib/systemd/system/birdnet-go.service ] \
     || [ -f "$RUN_HOME/birdnet-go-app/config/config.yaml" ]; then
  say "BirdNET-Go is already installed"
else
  say "Installing BirdNET-Go with its official installer (this takes a while)"
  bng="$(mktemp -d)"
  curl -fsSL https://github.com/tphakala/birdnet-go/raw/main/install.sh -o "$bng/install.sh"
  chown -R "$RUN_USER" "$bng"
  # The installer refuses to run as root and uses sudo itself; --silent takes settings from BIRDNET_* variables.
  # The location is written into its config.yaml later, when it is entered in the Base setup.
  (cd "$bng" && sudo -u "$RUN_USER" -H env BIRDNET_TELEMETRY=false bash ./install.sh -v "${WN_BIRDNET_VERSION:-latest}" --silent)
  rm -rf "$bng"
fi

CODE="$(python3 /opt/wnbase/wnbase.py --config /etc/wnbase/wnbase.ini --setup-code)"
SSID="$(python3 /opt/wnbase/wnbase.py --config /etc/wnbase/wnbase.ini --hotspot-env | cut -f2)"

if command -v rfkill >/dev/null && rfkill list wifi 2>/dev/null | grep -q "Soft blocked: yes"; then
  warn "Wi-Fi is blocked until a country is set: run again with WN_WIFI_COUNTRY=XX (two-letter code)"
fi

say "Done."
echo
echo "  Hotspot (Wi-Fi):  $SSID"
echo "  Password:         wn-$CODE"
echo "  Setup code:       $CODE"
echo "  Then open:        http://10.42.0.1"
echo
echo "Write these on the label of the Base. On a network the dashboard is also at http://$(hostname).local"
echo "Reboot to start the hotspot (or run: sudo /opt/wnbase/hotspot.sh --force, which drops a Wi-Fi SSH session)."
