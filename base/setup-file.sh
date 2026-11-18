#!/usr/bin/env bash
# Write the Base's setup details to wildnetwork-setup.txt on the boot partition, for owners without the label.
# Used by firstboot.sh (SD card image) and install.sh (stock Raspberry Pi OS).
# Usage: setup-file.sh <name> <ssid> <setup code> [boot folder]
set -euo pipefail
NAME="$1"; SSID="$2"; CODE="$3"
BOOT="${4:-}"
if [ -z "$BOOT" ]; then
  for d in /boot/firmware /boot; do
    if [ -f "$d/config.txt" ] || [ -f "$d/cmdline.txt" ]; then BOOT="$d"; break; fi
  done
fi
[ -n "$BOOT" ] && [ -d "$BOOT" ] || { echo "no boot partition found; setup details not written"; exit 0; }
cat > "$BOOT/wildnetwork-setup.txt" <<TXT
Wildlife Network Base

Name:        $NAME
Wi-Fi:       $SSID
Password:    wn-$CODE
Setup code:  $CODE
Then open:   http://10.42.0.1

Anyone holding this SD card can read this file. Keep the card inside the Base.
TXT
echo "Setup details written to $BOOT/wildnetwork-setup.txt"
