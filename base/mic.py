#!/usr/bin/env python3
"""mic.py: point BirdNET-Go at the Base's microphone.

BirdNET-Go's default audio device is "sysdefault", which on a Raspberry Pi is the onboard audio (card 0). That card
cannot record, so BirdNET-Go starts "without active audio sources". This script reads `arecord -l`, picks the first
USB capture device (or, without one, the first capture device of any kind) and writes it into BirdNET-Go's
config.yaml as ":<card>,<device>", the ALSA name BirdNET-Go accepts.

A device the owner chose is never changed: the config is only edited while it still says "sysdefault" (or is
empty), or still holds the value this script wrote last time (kept in the --state file), so a microphone that
moves to another card number after a reboot is followed. Standard library only; also imported by wnbase.py.

Usage:
  mic.py --config ~/birdnet-go-app/config/config.yaml [--state /var/lib/wnbase/mic-device] [--restart]
  mic.py --list                                  print the capture devices found
Exit status: 0 (also when nothing changed or no microphone was found), 2 on a usage or file error.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_DEVICES = {"", "sysdefault"}   # BirdNET-Go's default: safe to replace
BACKUP_SUFFIX = ".before-mic"
ARECORD_LINE = re.compile(r"^card (\d+): (\S+) \[(.*?)\], device (\d+): (.*?) \[(.*?)\]\s*$")


def parse_arecord(text: str, asound: str | None = None) -> list[dict]:
    """Capture devices from `arecord -l` output, in the order listed.

    Each: {card, device, id, name, usb, alsa}. A device counts as USB when its card or device name says USB, or
    when /proc/asound/card<N>/usbid exists (checked only when `asound` is given)."""
    out = []
    for line in text.splitlines():
        m = ARECORD_LINE.match(line.strip())
        if not m:
            continue
        card, cid, cname, dev, _, dname = m.groups()
        usb = "usb" in f"{cname} {dname}".lower()
        if not usb and asound:
            usb = os.path.exists(os.path.join(asound, f"card{card}", "usbid"))
        out.append({"card": int(card), "device": int(dev), "id": cid, "name": cname,
                    "usb": usb, "alsa": f":{card},{dev}"})
    return out


def pick(devices: list[dict]) -> dict | None:
    """The first USB capture device, else the first capture device, else None."""
    return next((d for d in devices if d["usb"]), devices[0] if devices else None)


def list_devices(run=None) -> list[dict] | None:
    """Capture devices on this machine, or None when `arecord` could not be run."""
    try:
        if run:
            rc, out = run(["arecord", "-l"], 10)
        else:
            p = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=10)
            rc, out = p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if rc == 127:   # not installed
        return None
    # Without any sound card arecord exits non-zero ("no soundcards found"): that is an answer, an empty list.
    return parse_arecord(out, "/proc/asound")


# --- config.yaml --------------------------------------------------------------------------------------------

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _blank(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


def _children(lines: list[str], start: int, end: int, parent_indent: int) -> tuple[int | None, list[int]]:
    """Line numbers of the direct children of a mapping whose body is lines[start:end]."""
    indent, kids = None, []
    for n in range(start, end):
        if _blank(lines[n]):
            continue
        ind = _indent(lines[n])
        if ind <= parent_indent:
            break
        if indent is None:
            indent = ind
        if ind == indent:
            kids.append(n)
    return indent, kids


def _block_end(lines: list[str], n: int, indent: int) -> int:
    """End (exclusive) of the block that starts at line n with the given indent."""
    for m in range(n + 1, len(lines)):
        if not _blank(lines[m]) and _indent(lines[m]) <= indent:
            return m
    return len(lines)


KEY = re.compile(r"^( *)(- +)?([A-Za-z0-9_]+)( *: *)([^#\r\n]*?)( +#[^\r\n]*)?(\r?\n?)$")


def _key(line: str):
    return KEY.match(line)


def _find(lines: list[str], start: int, end: int, parent_indent: int, name: str) -> int | None:
    _, kids = _children(lines, start, end, parent_indent)
    for n in kids:
        m = _key(lines[n])
        if m and not m.group(2) and m.group(3) == name:
            return n
    return None


def find_device_line(text: str) -> int | None:
    """Line number of the audio device in BirdNET-Go's config: realtime.audio.sources[0].device, or the older
    realtime.audio.source. No YAML library: walks the indentation, so comments and layout are kept."""
    lines = text.splitlines(keepends=True)
    rt = _find(lines, 0, len(lines), -1, "realtime")
    if rt is None:
        return None
    audio = _find(lines, rt + 1, _block_end(lines, rt, 0), 0, "audio")
    if audio is None:
        return None
    a_ind = _indent(lines[audio])
    a_end = _block_end(lines, audio, a_ind)
    srcs = _find(lines, audio + 1, a_end, a_ind, "sources")
    if srcs is not None:
        s_ind = _indent(lines[srcs])
        # the list items may sit at the same indent as "sources:" or deeper
        for n in range(srcs + 1, a_end):
            if _blank(lines[n]):
                continue
            if _indent(lines[n]) < s_ind or (_indent(lines[n]) == s_ind and not lines[n].lstrip().startswith("-")):
                break
            if lines[n].lstrip().startswith("-"):           # first item
                item_ind = _indent(lines[n])
                key_ind = item_ind + len(re.match(r"- *", lines[n].lstrip()).group(0))
                m = _key(lines[n])
                if m and m.group(3) == "device":
                    return n
                for k in range(n + 1, a_end):
                    if _blank(lines[k]):
                        continue
                    if _indent(lines[k]) < key_ind:
                        break
                    m = _key(lines[k])
                    if _indent(lines[k]) == key_ind and m and not m.group(2) and m.group(3) == "device":
                        return k
                break
    return _find(lines, audio + 1, a_end, a_ind, "source")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def read_device(text: str) -> str | None:
    """The configured audio device, or None when the config has no such key."""
    n = find_device_line(text)
    if n is None:
        return None
    return _unquote(_key(text.splitlines(keepends=True)[n]).group(5))


def set_device(text: str, device: str) -> str:
    """Text with the audio device set to `device` (quoted); only that one line changes."""
    lines = text.splitlines(keepends=True)
    n = find_device_line(text)
    if n is None:
        raise ValueError("no realtime.audio device in the BirdNET-Go config")
    m = _key(lines[n])
    lines[n] = (m.group(1) + (m.group(2) or "") + m.group(3) + m.group(4) + f'"{device}"' + (m.group(6) or "")
                + m.group(7))
    return "".join(lines)


def may_change(current: str | None, last_written: str | None) -> bool:
    """True when the device is still BirdNET-Go's default or the one this script wrote; never the owner's choice."""
    if current is None:
        return False
    return current in DEFAULT_DEVICES or (bool(last_written) and current == last_written)


def device_ok(configured: str | None, devices: list[dict]) -> bool:
    """Whether the configured device names a card that can record."""
    if not devices:
        return False
    if configured is None or configured in DEFAULT_DEVICES or configured == "default":
        return any(d["card"] == 0 for d in devices)       # sysdefault is card 0
    m = re.fullmatch(r"(?:plug)?(?:hw|plughw|sysdefault)?:(?:CARD=)?([^,]+)(?:,(?:DEV=)?(\d+))?", configured)
    if not m:
        return True   # a name we cannot read (an ALSA plugin, a PulseAudio device): trust the owner
    card, dev = m.group(1), m.group(2)
    for d in devices:
        if card in (str(d["card"]), d["id"]) and (dev is None or int(dev) == d["device"]):
            return True
    return False


def describe(d: dict) -> str:
    return f'{d["alsa"]} ({d["name"]}, {"USB" if d["usb"] else "not USB"})'


def write_keep_owner(path: str, data: bytes) -> None:
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".mic-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply(config: str, devices: list[dict], state: str | None = None) -> tuple[bool, str]:
    """Choose a device and write it into config. Returns (changed, message for the log)."""
    try:
        raw = Path(config).read_bytes()
    except OSError as e:
        raise SystemExit(f"BirdNET-Go config not readable: {e}")
    text = raw.decode("utf-8", "surrogateescape")
    current = read_device(text)
    last = None
    if state:
        try:
            last = Path(state).read_text().strip() or None
        except OSError:
            pass
    if current is None:
        return False, f"No audio device setting found in {config}; choose the microphone in BirdNET-Go (port 8080)"
    chosen = pick(devices)
    if not may_change(current, last):
        if chosen is None:
            return False, f'No microphone found. BirdNET-Go is set to "{current}" (chosen by you, left as it is)'
        return False, f'BirdNET-Go microphone left as you set it: "{current}"'
    if chosen is None:
        return False, ("WARNING: No microphone found (arecord -l lists no capture device). "
                       "Plug in a USB microphone and run this again, or restart BirdNET-Go")
    if current == chosen["alsa"]:
        return False, f"BirdNET-Go records from {describe(chosen)}"
    shutil.copy2(config, config + BACKUP_SUFFIX)
    write_keep_owner(config, set_device(text, chosen["alsa"]).encode("utf-8", "surrogateescape"))
    if state:
        os.makedirs(os.path.dirname(state) or ".", exist_ok=True)
        Path(state).write_text(chosen["alsa"] + "\n")
    return True, (f'BirdNET-Go microphone set to {describe(chosen)} (was "{current}"; '
                  f"backup in {config}{BACKUP_SUFFIX})")


def restart_birdnet() -> str:
    for args in (["systemctl", "restart", "birdnet-go.service"], ["docker", "restart", "birdnet-go"]):
        try:
            if subprocess.run(args, capture_output=True, timeout=180).returncode == 0:
                return " ".join(args)
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Point BirdNET-Go at the Base's microphone")
    ap.add_argument("--config", help="BirdNET-Go config.yaml")
    ap.add_argument("--state", help="file that remembers the device this script wrote")
    ap.add_argument("--restart", action="store_true", help="restart BirdNET-Go when the device changed")
    ap.add_argument("--list", action="store_true", help="only list the capture devices")
    ap.add_argument("--arecord-output", help=argparse.SUPPRESS)  # tests: read this file instead of running arecord
    a = ap.parse_args()
    if a.arecord_output:
        devices = parse_arecord(Path(a.arecord_output).read_text())
    else:
        devices = list_devices()
        if devices is None:
            print("WARNING: could not run arecord (install alsa-utils); microphone not checked")
            return
    if a.list:
        for d in devices:
            print("Capture device: " + describe(d))
        if not devices:
            print("Capture device: none found")
        return
    if not a.config:
        ap.error("--config is needed")
    if not os.path.isfile(a.config):
        print(f"BirdNET-Go config not found: {a.config}", file=sys.stderr)
        sys.exit(2)
    changed, msg = apply(a.config, devices, a.state)
    print(msg)
    if changed and a.restart:
        how = restart_birdnet()
        print(f"BirdNET-Go restarted ({how})" if how else "WARNING: could not restart BirdNET-Go; restart it yourself")


if __name__ == "__main__":
    main()
