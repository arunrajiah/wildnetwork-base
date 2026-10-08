#!/usr/bin/env bash
# Point BirdNET-Go at the first sound card that can record, while its config still has the default device
# "sysdefault" (on a Raspberry Pi that is the headphone output, which cannot record). A device the owner chose in
# BirdNET-Go is never changed. Runs before every BirdNET-Go start, so a microphone plugged in later is found too.
set -euo pipefail
CFG="${1:?usage: audio-detect.sh <config.yaml>}"
[ -f "$CFG" ] && grep -q 'device: "sysdefault"' "$CFG" || exit 0
card=""
for c in "${WN_ASOUND:-/proc/asound}"/card[0-9]*; do
  if compgen -G "$c/pcm*c" >/dev/null; then card="$(cat "$c/id")"; break; fi
done
[ -n "$card" ] || { echo "no microphone found"; exit 0; }
tmp="$(mktemp)"
sed "s/device: \"sysdefault\"/device: \"sysdefault:CARD=$card\"/" "$CFG" > "$tmp"
cat "$tmp" > "$CFG"   # keeps the file's owner and mode
rm -f "$tmp"
echo "BirdNET-Go will record from sound card $card"
