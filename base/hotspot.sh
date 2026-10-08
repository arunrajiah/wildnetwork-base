#!/usr/bin/env bash
# Keep the WildNetwork Base Wi-Fi hotspot up (NetworkManager).
#   SSID      WildNetwork-<last 4 characters of the hardware id>
#   password  wn-<setup code>
#   address   10.42.0.1 (NetworkManager shared mode also runs DHCP for phones)
# Run at boot and every 2 minutes by wn-hotspot.timer. If the Wi-Fi radio is already connected to a network
# (the wn-uplink connection, or one set up when the SD card was flashed), the hotspot stays off so that link is
# not dropped; set force = yes in [hotspot] of /etc/wnbase/wnbase.ini, or pass --force, to always run it.
set -euo pipefail

CONF="${WNBASE_CONFIG:-/etc/wnbase/wnbase.ini}"
WNBASE="${WNBASE_PY:-/opt/wnbase/wnbase.py}"
CON=wn-hotspot

command -v nmcli >/dev/null || { echo "nmcli not found: install network-manager"; exit 1; }
IFS=$'\t' read -r IFNAME SSID PSK FORCE < <(python3 "$WNBASE" --config "$CONF" --hotspot-env)
[ "${1:-}" = "--force" ] && FORCE=1

nmcli radio wifi on || true

# STATE and CONNECTION of the Wi-Fi interface, e.g. "connected:home-wifi". At boot the interface can take a few
# seconds to appear.
for _ in $(seq 30); do
  current="$(nmcli -t -f DEVICE,STATE,CONNECTION device status | awk -F: -v d="$IFNAME" '$1 == d { print $2 ":" $3 }')"
  case "$current" in ""|unavailable:*|unmanaged:*) sleep 1 ;; *) break ;; esac
done
if [ -z "$current" ]; then
  echo "no Wi-Fi interface $IFNAME"
  exit 1
fi
state="${current%%:*}"; active="${current#*:}"
if [ "$state" = connected ] && [ "$active" != "$CON" ] && [ "$FORCE" != 1 ]; then
  echo "$IFNAME is connected to '$active'; hotspot stays off"
  exit 0
fi

if nmcli -g NAME connection show | grep -Fxq "$CON"; then
  # Keep SSID and password in step with the hardware id and setup code.
  nmcli connection modify "$CON" 802-11-wireless.ssid "$SSID" wifi-sec.psk "$PSK" connection.interface-name "$IFNAME"
else
  nmcli connection add type wifi ifname "$IFNAME" con-name "$CON" ssid "$SSID" \
    802-11-wireless.mode ap 802-11-wireless.band bg \
    ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp wifi-sec.psk "$PSK" \
    connection.autoconnect yes connection.autoconnect-priority -10
fi

if [ "$active" != "$CON" ]; then
  nmcli connection up "$CON"
  echo "hotspot $SSID is up on $IFNAME (10.42.0.1)"
fi
