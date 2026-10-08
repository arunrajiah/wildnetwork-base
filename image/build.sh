#!/usr/bin/env bash
# Build the WildNetwork Base SD card image with the official pi-gen (Docker based).
#   image/build.sh                       result: image/deploy/<date>-wildnetwork-base.img.xz
# Needs Docker (privileged containers, loop devices) and about 15 GB of free disk.
# Options (environment variables):
#   WN_WIFI_COUNTRY   Wi-Fi regulatory country set in the image, needed for the hotspot (default GB)
#   WN_PRUNE_WORK=1   delete earlier stages' file systems during the build (saves about 3 GB)
#   WN_KEEP_PIGEN=1   keep image/.pi-gen afterwards (for CONTINUE=1 rebuilds)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
# shellcheck source=VERSIONS
source "$HERE/VERSIONS"
PIGEN="$HERE/.pi-gen"
STAGE="$PIGEN/stage-wildnetwork"

say() { printf '\033[1;36m%s\033[0m\n' "$*"; }

say "pi-gen $PI_GEN_REF"
rm -rf "$PIGEN"
git init -q "$PIGEN"
git -C "$PIGEN" fetch -q --depth 1 "$PI_GEN_REPO" "$PI_GEN_REF"
git -C "$PIGEN" checkout -q FETCH_HEAD

# Our stage, with the files it installs. Only stage-wildnetwork is exported as an image.
cp -R "$HERE/stage-wildnetwork" "$STAGE"
mkdir -p "$STAGE/00-wildnetwork/files/src" "$STAGE/01-birdnet-go/files"
cp -R "$ROOT/base" "$ROOT/systemd" "$ROOT/install.sh" "$ROOT/LICENSE" "$STAGE/00-wildnetwork/files/src/"
cp "$HERE/VERSIONS" "$STAGE/01-birdnet-go/files/VERSIONS"
touch "$PIGEN/stage2/SKIP_IMAGES"

# pi-gen insists on a first user password when the first-boot rename is off. This one is random, never stored
# or shown, and replaced on every Base at its first boot by another random password (base/firstboot.sh).
BUILD_PASS="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
CONFIG_FILE="$(mktemp)"   # outside the pi-gen folder, so it is not copied into the build container image
trap 'rm -f "$CONFIG_FILE"' EXIT
chmod 600 "$CONFIG_FILE"
cat > "$CONFIG_FILE" <<CONFIG
IMG_NAME=wildnetwork-base
RELEASE=trixie
LOCALE_DEFAULT=en_GB.UTF-8
TIMEZONE_DEFAULT=UTC
KEYBOARD_KEYMAP=us
KEYBOARD_LAYOUT="English (US)"
TARGET_HOSTNAME=wildnetwork-base
FIRST_USER_NAME=wildnetwork
FIRST_USER_PASS=$BUILD_PASS
DISABLE_FIRST_BOOT_USER_RENAME=1
PASSWORDLESS_SUDO=1
ENABLE_SSH=0
ENABLE_CLOUD_INIT=0
WPA_COUNTRY=${WN_WIFI_COUNTRY:-GB}
STAGE_LIST="stage0 stage1 stage2 stage-wildnetwork"
DEPLOY_COMPRESSION=xz
COMPRESSION_LEVEL=6
export WN_PRUNE_WORK=${WN_PRUNE_WORK:-0}
CONFIG
unset BUILD_PASS

say "Building (this takes a while)"
start=$(date +%s)
(cd "$PIGEN" && CONTAINER_NAME="${CONTAINER_NAME:-wnbase_pigen}" ./build-docker.sh -c "$CONFIG_FILE")
say "Build took $(( ($(date +%s) - start) / 60 )) minutes"

mkdir -p "$HERE/deploy"
find "$PIGEN/deploy" -maxdepth 1 -type f \( -name '*.img.xz' -o -name '*.log' -o -name '*.info' -o -name '*.json.xz' \) \
  -exec mv {} "$HERE/deploy/" \;
[ "${WN_KEEP_PIGEN:-0}" = 1 ] || rm -rf "$PIGEN"
ls -lh "$HERE/deploy"
