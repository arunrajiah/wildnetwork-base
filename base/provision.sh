#!/usr/bin/env bash
# Steps shared by the live installer (install.sh) and the SD card image build (image/stage-wildnetwork).
#   provision.sh --live    on a running Raspberry Pi: also reloads systemd and (re)starts the services
#   provision.sh --image   inside the image chroot: installs and enables only; nothing is started,
#                          no setup code is made (each Base makes its own at first boot)
# Environment: WN_USER (required), WN_BASE_URL, WN_SRC (repository root; default: the folder above this script).
set -euo pipefail

MODE="${1:-}"
case "$MODE" in --live|--image) ;; *) echo "usage: $0 --live|--image"; exit 2 ;; esac
say() { printf '\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

SRC="${WN_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BASE_URL="${WN_BASE_URL:-https://wildnetwork.arunrajiah.com}"
RUN_USER="${WN_USER:?WN_USER is required}"
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
APP_DIR="$RUN_HOME/birdnet-go-app"

# --- packages ------------------------------------------------------------------------------------------------
# dnsmasq-base: NetworkManager needs it for the hotspot's DHCP (shared mode). alsa-utils: microphone detection.
PKGS=(network-manager modemmanager dnsmasq-base sqlite3 python3 curl ca-certificates alsa-utils)
# The image carries BirdNET-Go as a container, so it needs Docker; BirdNET-Go's own installer adds it on a live install.
[ "$MODE" = --image ] && PKGS+=(docker.io)
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq "${PKGS[@]}" >/dev/null
if [ "$MODE" = --image ]; then
  usermod -aG docker,audio "$RUN_USER"
  # pi-gen installs cloud-init even when the image does not use it; keep it from changing the host name, users or
  # SSH keys at boot. The Base sets those itself at first boot.
  if [ -d /etc/cloud ]; then touch /etc/cloud/cloud-init.disabled; fi
fi

# --- wdx-agent -----------------------------------------------------------------------------------------------
install -d -m 755 /opt/wdx-agent /opt/wnbase
tmp="$(mktemp)"
if curl -fsSL "$BASE_URL/agent/wdx_agent.py" -o "$tmp" && grep -q "def pending_batch" "$tmp"; then
  install -m 755 "$tmp" /opt/wdx-agent/wdx_agent.py
  say "wdx-agent downloaded from $BASE_URL"
elif [ -f "$SRC/wdx_agent.py" ]; then
  install -m 755 "$SRC/wdx_agent.py" /opt/wdx-agent/wdx_agent.py
  warn "Download failed; using the copy of wdx_agent.py next to install.sh"
elif [ -f /opt/wdx-agent/wdx_agent.py ]; then
  warn "Download failed; keeping the wdx-agent already installed"
else
  rm -f "$tmp"; echo "Could not get wdx_agent.py (offline?). Put a copy next to install.sh and run again."; exit 1
fi
rm -f "$tmp"

# --- wnbase --------------------------------------------------------------------------------------------------
for f in wnbase.py hotspot.sh firstboot.sh ssh-key.sh birdnet-go-unit.sh audio-detect.sh; do
  install -m 755 "$SRC/base/$f" "/opt/wnbase/$f"
done

# --- configs (kept when present) -----------------------------------------------------------------------------
if [ ! -f /etc/wdx-agent.ini ]; then
  # In the image the station name is filled in at first boot, from the new host name.
  NAME="$( [ "$MODE" = --live ] && hostname || true )"
  cat > /etc/wdx-agent.ini <<INI
[agent]
endpoint = $BASE_URL/api/v1/events
api_key =
source = birdnet-go
# The Base's WildNetwork key only accepts this system name.
system = wildnetwork-base
sensor_model = WildNetwork Base
path = $APP_DIR/data/birdnet.db
station_name = $NAME
# latitude and longitude are added by the Base setup (wdx-agent cannot read them empty)
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
install -d -m 755 /etc/wnbase /var/lib/wnbase

if [ ! -f /etc/wnbase/wnbase.ini ]; then
  cat > /etc/wnbase/wnbase.ini <<INI
[wnbase]
port = 80
agent_config = /etc/wdx-agent.ini
agent_module = /opt/wdx-agent/wdx_agent.py
agent_service = wdx-agent
# Empty: the path in /etc/wdx-agent.ini
birdnet_db =
clips_dir = $APP_DIR/data/clips
# BirdNET-Go settings; wnbase writes the station location here so its species range filter is right
birdnet_config = $APP_DIR/config/config.yaml
birdnet_container = birdnet-go
setup_code_file = /etc/wnbase/setup-code

[hotspot]
ifname = wlan0
ssid_prefix = WildNetwork-
# yes: keep the hotspot up even when the Wi-Fi radio could join a known network
force = no
INI
  say "Wrote /etc/wnbase/wnbase.ini"
fi

# --- services ------------------------------------------------------------------------------------------------
for u in wnbase.service wn-hotspot.service wn-hotspot.timer; do
  install -m 644 "$SRC/systemd/$u" /etc/systemd/system/
done
sed "s/__USER__/$RUN_USER/" "$SRC/systemd/wdx-agent.service" > /etc/systemd/system/wdx-agent.service
chmod 644 /etc/systemd/system/wdx-agent.service
UNITS=(wnbase.service wn-hotspot.service wn-hotspot.timer wdx-agent.service)
if [ "$MODE" = --image ]; then
  for u in wn-firstboot.service wn-ssh-key.service; do
    sed "s/__USER__/$RUN_USER/" "$SRC/systemd/$u" > "/etc/systemd/system/$u"
    chmod 644 "/etc/systemd/system/$u"
  done
  UNITS+=(wn-firstboot.service wn-ssh-key.service docker.service)
fi
# Works in a chroot too: enabling only writes the symlinks.
systemctl enable "${UNITS[@]}" >/dev/null 2>&1

if [ "$MODE" = --live ]; then
  systemctl daemon-reload
  systemctl start wn-hotspot.timer
  systemctl restart wnbase.service
  systemctl restart wdx-agent.service || true
  # The hotspot is not started here: on a Base installed over Wi-Fi that would cut this session.
  # It starts at the next boot, or within 2 minutes once the Wi-Fi radio is free.
fi
