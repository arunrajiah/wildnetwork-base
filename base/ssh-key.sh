#!/usr/bin/env bash
# Key-only SSH, switched on by the owner: put a public key file named wildnetwork-ssh.pub on the boot partition.
# Read at every boot. With the file, its keys become the only way in (no passwords, no root login). When the file
# is removed again, SSH is switched back off at the next boot.
set -euo pipefail
RUN_USER="${1:?usage: ssh-key.sh <user>}"
KEYFILE=/boot/firmware/wildnetwork-ssh.pub
MARK=/var/lib/wnbase/ssh-enabled
HOME_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)"

if [ -s "$KEYFILE" ]; then
  keys="$(mktemp)"
  tr -d '\r' < "$KEYFILE" | grep -E '^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) ' > "$keys" || true
  if ! [ -s "$keys" ] || ! ssh-keygen -l -f "$keys" >/dev/null 2>&1; then
    echo "$KEYFILE holds no valid public key; SSH not changed"
    rm -f "$keys"; exit 0
  fi
  install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$HOME_DIR/.ssh"
  install -m 600 -o "$RUN_USER" -g "$RUN_USER" "$keys" "$HOME_DIR/.ssh/authorized_keys"
  rm -f "$keys"
  cat > /etc/ssh/sshd_config.d/10-wildnetwork.conf <<CONF
# WildNetwork Base: keys from wildnetwork-ssh.pub only
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers $RUN_USER
CONF
  compgen -G "/etc/ssh/ssh_host_*_key" >/dev/null || ssh-keygen -A
  systemctl enable ssh.service >/dev/null 2>&1
  systemctl start --no-block ssh.service || true
  touch "$MARK"
  echo "SSH on for $RUN_USER with the keys in $KEYFILE"
elif [ -f "$MARK" ]; then
  systemctl disable ssh.service >/dev/null 2>&1 || true
  systemctl stop --no-block ssh.service || true
  rm -f "$HOME_DIR/.ssh/authorized_keys" "$MARK"
  echo "$KEYFILE removed: SSH switched off"
fi
