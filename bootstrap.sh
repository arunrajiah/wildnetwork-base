#!/usr/bin/env bash
# Install the Wildlife Network Base software on a stock Raspberry Pi OS, without a keyboard or SSH session.
#
#   One command (on the Pi, as a user with sudo):
#     curl -fsSL https://raw.githubusercontent.com/arunrajiah/wildnetwork-base/main/bootstrap.sh | sudo bash
#   No keyboard: from cloud-init on the first boot (a line in the boot partition's user-data, see README.md):
#     bash -c "curl -fsSL https://raw.githubusercontent.com/arunrajiah/wildnetwork-base/main/bootstrap.sh | bash -s -- --service"
#
# --service sets up wn-install.service, which runs the install as root once the network is up, retries every
# 2 minutes until it succeeds, then disables itself. Without --service the install runs straight away.
# Settings come from the environment or from wildnetwork-firstboot.env on the boot partition (KEY=value lines):
#   WN_REF (git branch or tag, default main), WN_USER, WN_WIFI_COUNTRY, WN_BASE_URL, WN_BIRDNET_VERSION, WN_SKIP_BIRDNET
set -euo pipefail

REPO="${WN_REPO:-https://github.com/arunrajiah/wildnetwork-base}"
SRC=/opt/wnbase-src
UNIT=/etc/systemd/system/wn-install.service
DONE=/var/lib/wnbase/install-done
ENV_KEYS="WN_REF WN_USER WN_WIFI_COUNTRY WN_BASE_URL WN_BIRDNET_VERSION WN_SKIP_BIRDNET"
boot_dir() {
  for d in /boot/firmware /boot; do
    if [ -f "$d/config.txt" ] || [ -f "$d/cmdline.txt" ]; then echo "$d"; return; fi
  done
}

# KEY=value lines of wildnetwork-firstboot.env; only the keys above, nothing is executed. The environment wins.
load_env() {
  local file="$1" line key val
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    val="${val#\"}"; val="${val%\"}"; val="${val#\'}"; val="${val%\'}"
    case " $ENV_KEYS " in *" $key "*) ;; *) echo "ignoring unknown setting $key in $file"; continue ;; esac
    if [ -z "${!key:-}" ]; then export "$key=$val"; fi
  done < "$file"
}

if [ "${1:-}" = --print-env ]; then   # tests: show what a wildnetwork-firstboot.env sets
  load_env "$2"
  for k in $ENV_KEYS; do [ -n "${!k:-}" ] && echo "$k=${!k}"; done
  exit 0
fi
[ "$(id -u)" -eq 0 ] || { echo "Run as root: curl -fsSL .../bootstrap.sh | sudo bash"; exit 1; }

if [ "${1:-}" = --service ]; then
  cat > "$UNIT" <<UNIT
[Unit]
Description=Wildlife Network Base install (runs once, then disables itself)
Wants=network-online.target
After=network-online.target time-sync.target
ConditionPathExists=!$DONE

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/wn-bootstrap.sh
Restart=on-failure
RestartSec=120
TimeoutStartSec=90min

[Install]
WantedBy=multi-user.target
UNIT
  if [ -f "$0" ] && [ "$0" != bash ]; then install -m 755 "$0" /usr/local/sbin/wn-bootstrap.sh
  else curl -fsSL --retry 10 --retry-all-errors --retry-delay 10 "${REPO/github.com/raw.githubusercontent.com}/${WN_REF:-main}/bootstrap.sh" -o /usr/local/sbin/wn-bootstrap.sh
       chmod 755 /usr/local/sbin/wn-bootstrap.sh
  fi
  systemctl daemon-reload
  systemctl enable wn-install.service >/dev/null 2>&1
  systemctl start --no-block wn-install.service
  echo "wn-install.service set up; follow it with: journalctl -fu wn-install (log: /var/log/wnbase-install.log)"
  exit 0
fi

BOOT="$(boot_dir || true)"
[ -n "$BOOT" ] && load_env "$BOOT/wildnetwork-firstboot.env"
REF="${WN_REF:-main}"

# The network may still be coming up on a first boot.
for i in $(seq 1 30); do
  curl -fsS -o /dev/null --max-time 10 https://github.com && break
  [ "$i" = 30 ] && { echo "No internet connection; trying again later"; exit 1; }
  sleep 10
done
command -v git >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git >/dev/null; }
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch -q --depth 1 origin "$REF"
  git -C "$SRC" checkout -q --force FETCH_HEAD
else
  rm -rf "$SRC"
  git clone -q --depth 1 --branch "$REF" "$REPO" "$SRC"
fi
echo "Wildlife Network Base source: $REPO $REF ($(git -C "$SRC" rev-parse --short HEAD))"

bash "$SRC/install.sh"

if [ -f "$UNIT" ]; then
  install -d /var/lib/wnbase
  touch "$DONE"
  systemctl disable wn-install.service >/dev/null 2>&1 || true
  echo "wn-install.service finished and is now disabled"
fi
