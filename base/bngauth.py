#!/usr/bin/env python3
"""bngauth.py: switch on BirdNET-Go's own password for its settings, with a random password kept on the Base.

BirdNET-Go's web page on port 8080 is open to everyone on the Base's Wi-Fi. With `security.basicauth` on, its
settings, control actions (restart, reload) and changes to detections (delete, review, lock, ignore) need a login,
while reading detections stays open, so apps such as BirdEcho keep working without a password.

A password the owner set in BirdNET-Go is never changed: the config is only edited while basicauth is off and its
password empty. The password is stored in --password-file (root only) and reused when the config is reset.
BirdNET-Go 20260823 compares the password as plain text (internal/api/auth/adapter.go), so it is written as is.

Usage: bngauth.py --config ~/birdnet-go-app/config/config.yaml [--password-file /etc/wnbase/birdnet-go-password]
                  [--restart]
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mic  # noqa: E402  (the line-based YAML helpers)

PASSWORD_FILE = "/etc/wnbase/birdnet-go-password"
BACKUP_SUFFIX = ".before-auth"


def find_path(text: str, path: list[str]) -> int | None:
    """Line number of a nested mapping key (e.g. security.basicauth.enabled), or None."""
    lines = text.splitlines(keepends=True)
    start, end, indent, n = 0, len(lines), -1, None
    for key in path:
        n = mic._find(lines, start, end, indent, key)
        if n is None:
            return None
        indent = mic._indent(lines[n])
        start, end = n + 1, mic._block_end(lines, n, indent)
    return n


def get(text: str, path: list[str]) -> str | None:
    n = find_path(text, path)
    return None if n is None else mic._unquote(mic._key(text.splitlines(keepends=True)[n]).group(5))


def put(text: str, path: list[str], value: str) -> str:
    lines = text.splitlines(keepends=True)
    n = find_path(text, path)
    if n is None:
        raise ValueError(f"no {'.'.join(path)} in the BirdNET-Go config")
    m = mic._key(lines[n])
    lines[n] = m.group(1) + (m.group(2) or "") + m.group(3) + m.group(4) + value + (m.group(6) or "") + m.group(7)
    return "".join(lines)


def owner_set(text: str) -> bool:
    """True when the owner already protects BirdNET-Go (basicauth on, or a password of their own)."""
    return (get(text, ["security", "basicauth", "enabled"]) or "").lower() == "true" \
        or bool(get(text, ["security", "basicauth", "password"]))


def load_or_make_password(path: str) -> str:
    try:
        pw = Path(path).read_text().strip()
        if pw:
            return pw
    except OSError:
        pass
    pw = secrets.token_urlsafe(18)   # 24 characters, letters, digits, - and _: no YAML quoting issues
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pw + "\n")
    os.chmod(path, 0o600)
    return pw


def apply(config: str, password_file: str) -> tuple[bool, str]:
    text = Path(config).read_bytes().decode("utf-8", "surrogateescape")
    if find_path(text, ["security", "basicauth", "enabled"]) is None or \
            find_path(text, ["security", "basicauth", "password"]) is None:
        return False, ("WARNING: BirdNET-Go config has no security.basicauth section; its settings page stays open. "
                       "Set a password in BirdNET-Go (port 8080, security settings)")
    if owner_set(text):
        if get(text, ["security", "basicauth", "password"]) == _stored(password_file):
            return False, f"BirdNET-Go settings are password protected (password in {password_file})"
        return False, "BirdNET-Go settings password left as you set it"
    pw = load_or_make_password(password_file)
    new = put(text, ["security", "basicauth", "enabled"], "true")
    new = put(new, ["security", "basicauth", "password"], f'"{pw}"')
    shutil.copy2(config, config + BACKUP_SUFFIX)
    mic.write_keep_owner(config, new.encode("utf-8", "surrogateescape"))
    return True, (f"BirdNET-Go settings now need a password (stored in {password_file}; the dashboard shows it "
                  "after the setup code). Reading detections stays open.")


def _stored(path: str) -> str | None:
    try:
        return Path(path).read_text().strip() or None
    except OSError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Protect BirdNET-Go's settings with a password")
    ap.add_argument("--config", required=True)
    ap.add_argument("--password-file", default=PASSWORD_FILE)
    ap.add_argument("--restart", action="store_true", help="restart BirdNET-Go when the config changed")
    a = ap.parse_args()
    if not os.path.isfile(a.config):
        print(f"BirdNET-Go config not found: {a.config}", file=sys.stderr)
        sys.exit(2)
    changed, msg = apply(a.config, a.password_file)
    print(msg)
    if changed and a.restart:
        how = mic.restart_birdnet()
        print(f"BirdNET-Go restarted ({how})" if how else "WARNING: could not restart BirdNET-Go; restart it yourself")


if __name__ == "__main__":
    main()
