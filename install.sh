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
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
BIRDNET_DB="$RUN_HOME/birdnet-go-app/data/birdnet.db"
CLIPS_DIR="$RUN_HOME/birdnet-go-app/data/clips"
say "Installing the WildNetwork Base software for user $RUN_USER"

# --- packages ------------------------------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq network-manager modemmanager sqlite3 python3 curl ca-certificates >/dev/null
systemctl enable --now NetworkManager ModemManager >/dev/null 2>&1 || true

if [ -n "${WN_WIFI_COUNTRY:-}" ] && command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country "$WN_WIFI_COUNTRY"
fi

# --- wdx-agent -----------------------------------------------------------------------------------------------
install -d -m 755 /opt/wdx-agent /opt/wnbase
tmp="$(mktemp)"
if curl -fsSL "$BASE_URL/agent/wdx_agent.py" -o "$tmp" && grep -q "def pending_batch" "$tmp"; then
  install -m 755 "$tmp" /opt/wdx-agent/wdx_agent.py
  say "wdx-agent downloaded from $BASE_URL"
elif [ -f "$HERE/wdx_agent.py" ]; then
  install -m 755 "$HERE/wdx_agent.py" /opt/wdx-agent/wdx_agent.py
  warn "Download failed; using the copy of wdx_agent.py next to this script"
elif [ -f /opt/wdx-agent/wdx_agent.py ]; then
  warn "Download failed; keeping the wdx-agent already installed"
else
  rm -f "$tmp"; echo "Could not get wdx_agent.py (offline?). Put a copy next to install.sh and run again."; exit 1
fi
rm -f "$tmp"

# --- wnbase --------------------------------------------------------------------------------------------------
install -m 755 "$HERE/base/wnbase.py" /opt/wnbase/wnbase.py
install -m 755 "$HERE/base/hotspot.sh" /opt/wnbase/hotspot.sh

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
  # Location is set later through the Base setup and BirdNET-Go's own web page (port 8080).
  (cd "$bng" && sudo -u "$RUN_USER" -H env BIRDNET_TELEMETRY=false bash ./install.sh -v "${WN_BIRDNET_VERSION:-latest}" --silent)
  rm -rf "$bng"
fi

# --- configs (kept when present) -----------------------------------------------------------------------------
if [ ! -f /etc/wdx-agent.ini ]; then
  cat > /etc/wdx-agent.ini <<INI
[agent]
endpoint = $BASE_URL/api/v1/events
api_key =
source = birdnet-go
path = $BIRDNET_DB
station_name = $(hostname)
latitude =
longitude =
round_coords = 2
min_confidence = 0.7
interval_seconds = 60
status_interval_seconds = 900
state_file = /var/lib/wdx-agent/state.json
INI
  say "Wrote /etc/wdx-agent.ini"
else
  say "Keeping /etc/wdx-agent.ini"
fi
chmod 640 /etc/wdx-agent.ini
chown "root:$RUN_GROUP" /etc/wdx-agent.ini
install -d -m 755 -o "$RUN_USER" -g "$RUN_GROUP" /var/lib/wdx-agent

install -d -m 755 /etc/wnbase
if [ ! -f /etc/wnbase/wnbase.ini ]; then
  cat > /etc/wnbase/wnbase.ini <<INI
[wnbase]
port = 80
agent_config = /etc/wdx-agent.ini
agent_module = /opt/wdx-agent/wdx_agent.py
agent_service = wdx-agent
# Empty: the path in /etc/wdx-agent.ini
birdnet_db =
clips_dir = $CLIPS_DIR
setup_code_file = /etc/wnbase/setup-code

[hotspot]
ifname = wlan0
ssid_prefix = WildNetwork-
# yes: keep the hotspot up even when the Wi-Fi radio could join a known network
force = no
INI
  say "Wrote /etc/wnbase/wnbase.ini"
fi
CODE="$(python3 /opt/wnbase/wnbase.py --config /etc/wnbase/wnbase.ini --setup-code)"
SSID="$(python3 /opt/wnbase/wnbase.py --config /etc/wnbase/wnbase.ini --hotspot-env | cut -f2)"

# --- services ------------------------------------------------------------------------------------------------
install -m 644 "$HERE/systemd/wnbase.service" "$HERE/systemd/wn-hotspot.service" "$HERE/systemd/wn-hotspot.timer" /etc/systemd/system/
sed "s/__USER__/$RUN_USER/" "$HERE/systemd/wdx-agent.service" > /etc/systemd/system/wdx-agent.service
chmod 644 /etc/systemd/system/wdx-agent.service
systemctl daemon-reload
systemctl enable wn-hotspot.service wdx-agent.service >/dev/null
systemctl enable --now wn-hotspot.timer >/dev/null
systemctl enable wnbase.service >/dev/null
systemctl restart wnbase.service
systemctl restart wdx-agent.service || true
# The hotspot is not started here: on a Base installed over Wi-Fi that would cut this session.
# It starts at the next boot, or within 2 minutes once the Wi-Fi radio is free.

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
