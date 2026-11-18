#!/usr/bin/env bash
# Check that BirdNET-Go's installer really finished: its config.yaml is there and the birdnet-go container exists.
# The container is started by the birdnet-go service (docker run --rm), so it can take a moment to appear: waits up
# to <wait> seconds (default 90). Prints what is missing and exits 1 when something is.
# Usage: birdnet-go-check.sh <config.yaml> [wait seconds] [container name]
set -uo pipefail
CFG="${1:?usage: birdnet-go-check.sh <config.yaml> [wait] [container]}"
WAIT="${2:-90}"
NAME="${3:-birdnet-go}"
missing=()
[ -f "$CFG" ] || missing+=("config file $CFG")
end=$((SECONDS + WAIT))
until docker container inspect "$NAME" >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$end" ]; then missing+=("Docker container $NAME"); break; fi
  sleep 2
done
if [ "${#missing[@]}" -gt 0 ]; then
  printf 'BirdNET-Go is not installed completely. Missing:\n'
  printf '  - %s\n' "${missing[@]}"
  exit 1
fi
echo "BirdNET-Go is installed: $CFG and container $NAME found"
