#!/usr/bin/env bash
# First boot of a WildNetwork Base SD card image (wn-firstboot.service, runs once).
# Quick part, then READY so the dashboard and hotspot can start: random password for the user (never shown),
# setup code, host name, SSH host keys, setup details on the console and the boot partition.
# Slow part: load the BirdNET-Go container that ships in the image and start it. No internet needed.
set -euo pipefail
RUN_USER="${1:?usage: firstboot.sh <user>}"
DONE=/var/lib/wnbase/firstboot-done
CONF=/etc/wnbase/wnbase.ini
WNBASE=/opt/wnbase/wnbase.py
TAR=/var/lib/wnbase/birdnet-go.tar
IMAGE="$(cat /etc/wnbase/birdnet-go-image)"
HOME_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)"
APP="$HOME_DIR/birdnet-go-app"
[ -f "$DONE" ] && exit 0

# 1. No shared password: a long random one nobody sees. Login is by SSH key only (see ssh-key.sh).
echo "$RUN_USER:$(head -c 48 /dev/urandom | base64 | tr -d '\n')" | chpasswd

# 2. Setup code, hotspot name and host name, all from this Base's hardware.
CODE="$(python3 "$WNBASE" --config "$CONF" --setup-code)"
SSID="$(python3 "$WNBASE" --config "$CONF" --hotspot-env | cut -f2)"
HW="$(python3 "$WNBASE" --hardware-id)"
NAME="wildnetwork-base-$(printf '%s' "${HW: -4}" | tr '[:upper:]' '[:lower:]')"
hostnamectl set-hostname "$NAME" 2>/dev/null || echo "$NAME" > /etc/hostname
hosts="$(grep -v '^127\.0\.1\.1' /etc/hosts || true)"
printf '%s\n127.0.1.1\t%s\n' "$hosts" "$NAME" > /etc/hosts
sed -i "s/^station_name *=[[:space:]]*$/station_name = $NAME/" /etc/wdx-agent.ini

# 3. SSH host keys unique to this Base (SSH itself stays off unless the owner adds a key).
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A >/dev/null

# 4. Setup details for owners without the label: HDMI console and the boot partition.
mkdir -p /etc/issue.d
cat > /etc/issue.d/wildnetwork.issue <<ISSUE
WildNetwork Base
  Name:        $NAME
  Wi-Fi:       $SSID
  Password:    wn-$CODE
  Setup code:  $CODE
  Then open:   http://10.42.0.1

ISSUE
if [ -d /boot/firmware ]; then
  cat > /boot/firmware/wildnetwork-setup.txt <<TXT
WildNetwork Base

Name:        $NAME
Wi-Fi:       $SSID
Password:    wn-$CODE
Setup code:  $CODE
Then open:   http://10.42.0.1

Anyone holding this SD card can read this file. Keep the card inside the Base.
TXT
fi

# The root file system is grown to the full card by Raspberry Pi OS itself on the first boot (rpi-resize).
systemd-notify --ready 2>/dev/null || true
echo "setup ready: $SSID"

# 5. BirdNET-Go: load the container image that ships with this SD card, then lay out its folders and unit the
#    way BirdNET-Go's installer does.
if [ -f "$TAR" ]; then
  docker load -i "$TAR"
  rm -f "$TAR"
fi
install -d -o "$RUN_USER" -g "$RUN_USER" "$APP" "$APP/config" "$APP/data" "$APP/data/clips"
if [ ! -f "$APP/config/config.yaml" ]; then
  install -m 644 -o "$RUN_USER" -g "$RUN_USER" /opt/wnbase/birdnet-go-config.yaml "$APP/config/config.yaml"
fi
/opt/wnbase/birdnet-go-unit.sh "$RUN_USER" "$IMAGE" > /etc/systemd/system/birdnet-go.service
systemctl daemon-reload
systemctl enable birdnet-go.service >/dev/null 2>&1
systemctl start --no-block birdnet-go.service

touch "$DONE"
echo "first boot done"
