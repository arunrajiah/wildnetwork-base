#!/usr/bin/env bash
# Print the systemd unit that runs BirdNET-Go, with the same docker run arguments as BirdNET-Go's own install.sh
# (generate_systemd_service_content): web port 8080, /dev/snd, config and data folders, thermal sensors, host
# Avahi and D-Bus sockets read-only, and the external media mount. Usage: birdnet-go-unit.sh <user> <image>
set -euo pipefail
RUN_USER="$1"; IMAGE="$2"
HOME_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)"
CONFIG_DIR="$HOME_DIR/birdnet-go-app/config"
DATA_DIR="$HOME_DIR/birdnet-go-app/data"
HOST_UID="$(id -u "$RUN_USER")"; HOST_GID="$(id -g "$RUN_USER")"
TZ="$(cat /etc/timezone 2>/dev/null || true)"; TZ="${TZ:-UTC}"

# Optional mounts, gated like the official installer: only when the host has them.
EXTRA=""
for m in "/sys/class/thermal:/sys/class/thermal" "/run/avahi-daemon:/run/avahi-daemon:ro" "/run/dbus:/run/dbus:ro"; do
  [ -d "${m%%:*}" ] && EXTRA+="    -v $m \\"$'\n'
done
cat <<UNIT
[Unit]
Description=BirdNET-Go
After=docker.service
Requires=docker.service
RequiresMountsFor=${CONFIG_DIR}/hls

[Service]
Restart=always
ExecStartPre=-/usr/bin/docker rm -f birdnet-go
ExecStartPre=/bin/mkdir -p ${CONFIG_DIR}/hls
ExecStartPre=/bin/sh -c 'mount -t tmpfs -o size=50M,mode=0755,uid=${HOST_UID},gid=${HOST_GID},noexec,nosuid,nodev tmpfs ${CONFIG_DIR}/hls || true'
ExecStartPre=-/bin/mkdir -p /mnt/birdnet-go/external
ExecStartPre=-/bin/sh -c 'mountpoint -q /mnt/birdnet-go/external || mount --bind /mnt/birdnet-go/external /mnt/birdnet-go/external'
ExecStartPre=-/bin/sh -c 'mount --make-rshared /mnt/birdnet-go/external'
ExecStartPre=-/bin/chown -h ${HOST_UID}:${HOST_GID} /mnt/birdnet-go/external
# Disable Wi-Fi power saving, as BirdNET-Go's installer does on a Raspberry Pi
ExecStartPre=-/bin/sh -c 'for i in /sys/class/net/wlan*; do [ -d "\$\$i" ] && iw dev "\$\$(basename "\$\$i")" set power_save off; done; true'
# WildNetwork Base: point BirdNET-Go at the first microphone while its config still says "sysdefault"
ExecStartPre=-/opt/wnbase/audio-detect.sh ${CONFIG_DIR}/config.yaml
ExecStart=/usr/bin/docker run --rm \\
    --name birdnet-go \\
    -p 8080:8080 \\
    --env TZ="${TZ}" \\
    --env BIRDNET_UID=${HOST_UID} \\
    --env BIRDNET_GID=${HOST_GID} \\
    --device /dev/snd \\
    -v ${CONFIG_DIR}:/config \\
    -v ${DATA_DIR}:/data \\
${EXTRA}    -v /mnt/birdnet-go/external:/external:rslave \\
    ${IMAGE}
ExecStopPost=/bin/sh -c 'umount -f ${CONFIG_DIR}/hls || true'
ExecStopPost=-/usr/bin/docker rm -f birdnet-go

[Install]
WantedBy=multi-user.target
UNIT
