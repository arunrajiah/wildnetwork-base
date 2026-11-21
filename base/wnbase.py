#!/usr/bin/env python3
"""wnbase: the local web service of a Wildlife Network Base.

Serves a dashboard and a small JSON API over the Base's own Wi-Fi hotspot (http://10.42.0.1) for the
Wildlife Network field app and for any browser. Detections, cursors and health come from wdx-agent, which is
imported from its own file so there is one copy of that logic. Standard library only.

Usage:
  wnbase.py --config /etc/wnbase/wnbase.ini          run the service
  wnbase.py --config ... --setup-code                print the setup code (created on first use)
  wnbase.py --config ... --hotspot-env               print "ifname<TAB>ssid<TAB>password<TAB>force" for hotspot.sh
  wnbase.py --hardware-id                            print the hardware id (Raspberry Pi serial number)
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import mic  # mic.py next to this file: arecord parsing and BirdNET-Go's audio device setting
except ImportError:  # an install that has not copied mic.py yet
    mic = None
try:
    import tzgrid  # tzgrid.py + tzgrid.dat next to this file: offline time zone of a location
except ImportError:
    tzgrid = None
try:
    import wnupdate  # wnupdate.py next to this file: automatic updates (its switch and last result)
except ImportError:
    wnupdate = None

VERSION = "0.3.0"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I, L, O, 0 or 1
CODE_LENGTH = 8
FAIL_LIMIT = 10        # wrong setup codes allowed per client address ...
FAIL_WINDOW = 600      # ... in this many seconds
MAX_BODY = 64 * 1024
RECENT_MAX = 200
REGISTER_EVERY = 300        # seconds between registration checks
REGISTER_MAX_BACKOFF = 3600
HEALTH_EVERY = 900          # seconds between health reports to Wildlife Network
HEALTH_QUEUE_MAX = 96       # reports kept while offline (one day); older ones are dropped
MIC_CHECK_EVERY = 60        # seconds the microphone check is cached
ALERTS_FOREVER = "2099-01-01T00:00:00Z"   # "until I turn them back on"
TZ_FILE = "wildnetwork-timezone.txt"
TZ_AUTO = ("", "auto", "automatic", "location")
TZ_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}")
# TZ in BirdNET-Go's unit: `--env TZ="Asia/Kolkata"` (its installer and ours), also -e and without quotes.
UNIT_TZ = re.compile(r'(--env|-e)(\s+|=)TZ=("?)([^"\s\\]*)\3')
# BirdNET-Go log lines (lowercase) that tell whether it records: the newest one wins
MIC_LOG_BAD = ("starting without active audio sources", "audio card capture failed")
MIC_LOG_OK = ("capture started",)
AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac", ".aac": "audio/aac",
               ".m4a": "audio/mp4", ".opus": "audio/ogg", ".ogg": "audio/ogg"}


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


class Config:
    """Settings of wnbase itself (/etc/wnbase/wnbase.ini). Keyword arguments override the file (used by tests)."""

    def __init__(self, path: str | None = None, **overrides):
        cp = configparser.ConfigParser(interpolation=None)
        if path:
            cp.read(path)
        w = cp["wnbase"] if cp.has_section("wnbase") else {}
        b = cp["base"] if cp.has_section("base") else {}
        self.path = path
        h = cp["hotspot"] if cp.has_section("hotspot") else {}

        def g(sec, key, default):
            return (sec.get(key) or "").strip() or default

        self.port = int(g(w, "port", "80"))
        self.bind = g(w, "bind", "0.0.0.0")
        self.agent_config = g(w, "agent_config", "/etc/wdx-agent.ini")
        self.agent_module = g(w, "agent_module", "/opt/wdx-agent/wdx_agent.py")
        self.agent_service = g(w, "agent_service", "wdx-agent")
        self.birdnet_db = g(w, "birdnet_db", "")      # empty: the `path` in the wdx-agent config
        self.clips_dir = g(w, "clips_dir", "")        # empty: "clips" next to the database
        self.birdnet_config = g(w, "birdnet_config", "")  # empty: ../config/config.yaml from the database folder
        self.birdnet_container = g(w, "birdnet_container", "birdnet-go")
        self.setup_code_file = g(w, "setup_code_file", "/etc/wnbase/setup-code")
        self.state_dir = g(w, "state_dir", "/var/lib/wnbase")
        self.avahi_service_file = g(w, "avahi_service_file", "/etc/avahi/services/wlnw-base.service")
        self.birdnet_password_file = g(w, "birdnet_password_file", "/etc/wnbase/birdnet-go-password")   # queued messages for Wildlife Network
        # BirdNET-Go's systemd unit (its TZ follows the Base's time zone) and the host's zone link
        self.birdnet_unit = g(w, "birdnet_unit", "/etc/systemd/system/birdnet-go.service")
        self.birdnet_unit_lib = g(w, "birdnet_unit_lib", "/lib/systemd/system/birdnet-go.service")
        self.birdnet_service = g(w, "birdnet_service", "birdnet-go")
        self.localtime = g(w, "localtime", "/etc/localtime")
        self.etc_timezone = g(w, "etc_timezone", "/etc/timezone")
        # [base] timezone: an IANA zone that wins over the one looked up from the location (empty: looked up)
        self.timezone = g(b, "timezone", "")
        # [base] auto_update: install new versions of the Base software and wdx-agent by themselves (default yes)
        self.auto_update = g(b, "auto_update", "yes")
        self.boot_dir = g(w, "boot_dir", "")   # empty: /boot/firmware or /boot, where config.txt is
        self.hotspot_ifname = g(h, "ifname", "wlan0")
        self.hotspot_ssid_prefix = g(h, "ssid_prefix", "Wildlife Network-")
        self.hotspot_force = g(h, "force", "no").lower() in ("1", "yes", "true", "on")
        for k, v in overrides.items():
            setattr(self, k, v)


def load_agent(path: str):
    spec = importlib.util.spec_from_file_location("wdx_agent", path)
    if spec is None or spec.loader is None:
        sys.exit(f"wdx-agent not found at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wdx_agent"] = mod
    spec.loader.exec_module(mod)
    for name in ("Settings", "pending_batch", "save_cursor", "collect_status", "load_state", "state_key"):
        if not hasattr(mod, name):
            sys.exit(f"wdx-agent at {path} is too old (missing {name}); install 0.3.0 or later")
    return mod


def hardware_id() -> str:
    """Raspberry Pi serial number, else the systemd machine id, else a hash of the host name."""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("serial") and ":" in line:
                serial = line.split(":", 1)[1].strip().lower()
                if serial.strip("0"):
                    return serial
    except OSError:
        pass
    try:
        mid = Path("/etc/machine-id").read_text().strip()
        if mid:
            return mid
    except OSError:
        pass
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]


def hotspot_ssid(cfg: Config, hw: str) -> str:
    return f"{cfg.hotspot_ssid_prefix}{hw[-4:].upper()}"


def ensure_setup_code(path: str) -> tuple[str, bool]:
    """Return (code, created). The code is made once per device and kept in a root-only file."""
    p = Path(path)
    try:
        code = p.read_text().strip().upper()
        if len(code) == CODE_LENGTH and all(c in CODE_ALPHABET for c in code):
            return code, False
    except FileNotFoundError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(code + "\n")
    os.replace(tmp, path)
    return code, True


def run_command(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a command without a shell. Missing programs give (127, '')."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr if r.returncode else "")
    except FileNotFoundError:
        return 127, ""
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def nmcli_fields(line: str) -> list[str]:
    """Split one `nmcli -t` line on unescaped colons."""
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in re.split(r"(?<!\\):", line)]


def write_atomic(path: str, data: bytes, new_mode: int = 0o640) -> None:
    """Replace a file atomically, keeping its mode and owner (new files get new_mode)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None
    tmp = f"{path}.wnbase-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if st is not None:
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.chmod(tmp, st.st_mode & 0o777)
    else:
        os.chmod(tmp, new_mode)
    os.replace(tmp, path)


def write_ini(path: str, section: str, updates: dict[str, str]) -> None:
    """Change some keys of an ini file atomically, keeping every other key, the file mode and the owner."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(path)
    if not cp.has_section(section):
        cp.add_section(section)
    for k, v in updates.items():
        cp[section][k] = v
    buf = io.StringIO()
    cp.write(buf)
    write_atomic(path, buf.getvalue().encode())


def set_yaml_keys(text: str, section: str, values: dict[str, str]) -> tuple[str, set[str]]:
    """Set scalar keys directly under a top-level YAML mapping, editing only those lines.

    No YAML library: finds the line `section:` at column 0, takes the following indented lines as its body, and
    rewrites `key: value` lines at the body's own indent (not deeper), keeping indentation, any trailing comment
    and the line ending. Everything else is left byte for byte. Returns (new text, keys that were found)."""
    lines = text.splitlines(keepends=True)
    head = re.compile(rf"{re.escape(section)}:[ \t]*(#.*)?\r?\n?")
    found: set[str] = set()
    i = next((n for n, line in enumerate(lines) if head.fullmatch(line)), None)
    if i is None:
        return text, found
    indent = None
    for n in range(i + 1, len(lines)):
        line = lines[n]
        bare = line.strip()
        if not bare or bare.startswith("#"):
            continue
        lead = line[:len(line) - len(line.lstrip(" \t"))]
        if not lead:
            break  # next top-level key: the section ended
        if indent is None:
            indent = lead
        if lead != indent:
            continue  # nested deeper (or odd indentation): not a direct key of this section
        m = re.fullmatch(r"([ \t]*)([A-Za-z0-9_]+)([ \t]*:[ \t]*)([^#\r\n]*?)([ \t]+#[^\r\n]*)?(\r?\n?)", line)
        if m and m.group(2) in values and m.group(2) not in found:
            lines[n] = m.group(1) + m.group(2) + m.group(3) + values[m.group(2)] + (m.group(5) or "") + m.group(6)
            found.add(m.group(2))
    return "".join(lines), found


MDNS_TYPE = "_wlnw-base._tcp"


def avahi_service_xml(port: int, txt: dict[str, str]) -> str:
    """Avahi service file advertising wnbase as _wlnw-base._tcp, named after the host, with TXT records."""
    from xml.sax.saxutils import escape
    records = "".join(f"    <txt-record>{escape(k)}={escape(v)}</txt-record>\n" for k, v in txt.items())
    return ('<?xml version="1.0" standalone="no"?>\n<!DOCTYPE service-group SYSTEM "avahi-service.dtd">\n'
            "<!-- Written by wnbase; rewritten when the station name changes -->\n<service-group>\n"
            '  <name replace-wildcards="yes">%h</name>\n  <service>\n'
            f"    <type>{MDNS_TYPE}</type>\n    <port>{int(port)}</port>\n{records}  </service>\n</service-group>\n")


def http_post_json(url: str, body, timeout: int = 30, token: str | None = None) -> tuple[int, dict]:
    """POST JSON, return (status, JSON body or {}). Network problems raise OSError (URLError is one)."""
    headers = {"content-type": "application/json", "user-agent": f"wnbase/{VERSION}"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {}


def canon(cursor) -> str:
    return json.dumps(cursor, sort_keys=True, separators=(",", ":"))


def cursor_rank(cursor):
    """Comparable position of a cursor where the shape is known (BirdNET-Go {"d": id} or a plain row id)."""
    if isinstance(cursor, dict) and set(cursor) == {"d"} and isinstance(cursor["d"], int):
        return ("d", cursor["d"])
    if isinstance(cursor, int) and not isinstance(cursor, bool):
        return ("n", cursor)
    return None


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def open_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True, timeout=10)


def valid_zone(name) -> str | None:
    """The IANA zone name when this system knows it (e.g. "Asia/Kolkata"), else None."""
    name = (name or "").strip() if isinstance(name, str) else ""
    if len(name) > 64 or not TZ_NAME.fullmatch(name):
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def read_zone_file(text: str) -> str:
    """wildnetwork-timezone.txt: a zone name, alone or as `timezone=` (also `timezone:`, `tz=`). Comments ignored."""
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=") if "=" in line else line.partition(":")
        if sep and k.strip().lower().replace(" ", "") in ("timezone", "tz", "zone"):
            line = v.strip()
        return line.strip("\"'").strip()
    return ""


def unit_zone(text: str) -> str | None:
    """The TZ BirdNET-Go's unit passes to its container, or None when it passes none."""
    m = UNIT_TZ.search(text)
    return m.group(4) if m else None


def set_unit_zone(text: str, zone: str) -> str:
    return UNIT_TZ.sub(lambda m: f"{m.group(1)}{m.group(2)}TZ={m.group(3)}{zone}{m.group(3)}", text)


class App:
    def __init__(self, cfg: Config, runner=None, agent=None, http_post=None, tz_lookup=None):
        self.cfg = cfg
        # Offline lookup of the time zone at a location (tests pass their own)
        self.tz_lookup = tz_lookup or (tzgrid.timezone_at if tzgrid else None)
        self.tz_lock = threading.Lock()
        self.http_post = http_post or http_post_json
        self.registration = "waiting-for-setup"
        self.reg_backoff = REGISTER_EVERY
        self.reg_wake = threading.Event()
        self.agent = agent or load_agent(cfg.agent_module)
        self.run = runner or run_command
        self.setup_code, created = ensure_setup_code(cfg.setup_code_file)
        if created:
            log(f"new setup code {self.setup_code} (stored in {cfg.setup_code_file}; hotspot password is wn-{self.setup_code})")
        self.hw = hardware_id()
        self.lock = threading.Lock()
        self.served_cursor: str | None = None
        self.fails: dict[str, deque] = defaultdict(deque)
        self._bng: tuple[float, str | None] | None = None
        self._mic: tuple[float, dict] | None = None

    # --- helpers -----------------------------------------------------------------------------------------

    def settings(self):
        try:
            return self.agent.Settings(self.cfg.agent_config)
        except SystemExit as e:
            raise ApiError(503, f"wdx-agent config: {e}")
        except ValueError as e:  # e.g. "latitude =" left empty: wdx-agent cannot parse it
            raise ApiError(503, f"wdx-agent config has a value it cannot read: {e}")

    def db_path(self, s) -> str:
        return self.cfg.birdnet_db or s.path

    def birdnet_config(self, s) -> str:
        return self.cfg.birdnet_config or os.path.join(os.path.dirname(os.path.dirname(self.db_path(s))),
                                                        "config", "config.yaml")

    def set_birdnet_location(self, lat: float | None, lon: float | None, restart: bool = True) -> list[str]:
        """Give BirdNET-Go the exact location so its range filter works. Unrounded: this stays on the device.
        restart=False: only write it (the caller restarts BirdNET-Go); self.birdnet_location_changed tells."""
        self.birdnet_location_changed = False
        try:
            path = self.birdnet_config(self.settings())
        except ApiError as e:
            return [f"BirdNET-Go location not set: {e.msg}"]
        values = {k: repr(v) for k, v in (("latitude", lat), ("longitude", lon)) if v is not None}
        try:
            raw = Path(path).read_bytes()
        except FileNotFoundError:
            return [f"BirdNET-Go config not found at {path}; set the location in BirdNET-Go yourself"]
        except OSError as e:
            return [f"BirdNET-Go config not readable: {e}"]
        text = raw.decode("utf-8", "surrogateescape")
        new, found = set_yaml_keys(text, "birdnet", values)
        notes = []
        missing = set(values) - found
        if missing:
            notes.append(f"BirdNET-Go config has no {' or '.join(sorted(missing))} under birdnet:; set it in BirdNET-Go")
        if new != text:
            write_atomic(path, new.encode("utf-8", "surrogateescape"))
            log(f"BirdNET-Go location updated in {path}")
            self.birdnet_location_changed = True
            if restart:
                notes += self.restart_birdnet()
        return notes

    def restart_birdnet(self) -> list[str]:
        rc, out = self.run(["docker", "restart", self.cfg.birdnet_container], 120)
        return [] if rc == 0 else [f"BirdNET-Go not restarted: {out.strip()[:200] or 'docker not available'}"]

    # --- time zone: the Base and BirdNET-Go keep local time, so detections are labelled the way the owner reads them

    def system_zone(self) -> str | None:
        """The host's time zone: the /etc/localtime link, else timedatectl."""
        try:
            target = os.path.realpath(self.cfg.localtime)
        except OSError:
            target = ""
        if "/zoneinfo/" in target:
            z = valid_zone(target.split("/zoneinfo/", 1)[1])
            if z:
                return z
        rc, out = self.run(["timedatectl", "show", "-p", "Timezone", "--value"], 10)
        return valid_zone(out.strip()) if rc == 0 else None

    def located_zone(self, s) -> str | None:
        """The zone at the station's location, looked up offline."""
        if s is None or s.latitude is None or s.longitude is None or self.tz_lookup is None:
            return None
        try:
            return valid_zone(self.tz_lookup(s.latitude, s.longitude))
        except (ValueError, OSError) as e:
            log(f"time zone lookup failed: {e}")
            return None

    def explicit_zone(self) -> str | None:
        """[base] timezone in wnbase.ini (set there, by POST /api/config or by wildnetwork-timezone.txt)."""
        raw = self.cfg.timezone
        if self.cfg.path:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(self.cfg.path)
            raw = cp.get("base", "timezone", fallback="")
        raw = (raw or "").strip()
        if raw.lower() in TZ_AUTO:
            return None
        z = valid_zone(raw)
        if not z:
            log(f"[base] timezone {raw!r} is not a time zone this system knows; using the location's")
        return z

    def store_zone(self, zone: str | None) -> None:
        """Keep an explicit zone (None: back to the location's) in wnbase.ini [base] timezone."""
        self.cfg.timezone = zone or ""
        if self.cfg.path:
            with self.lock:
                write_ini(self.cfg.path, "base", {"timezone": zone or ""})
        log(f"time zone setting: {zone or 'from the location'}")

    def boot_dir(self) -> str | None:
        return self.cfg.boot_dir or next((d for d in ("/boot/firmware", "/boot") if os.path.isfile(f"{d}/config.txt")), None)

    def apply_timezone_file(self) -> None:
        """wildnetwork-timezone.txt on the boot partition: store it as the time zone setting, once. The file is
        renamed to .applied with the result, so a zone set later in Setup is not overwritten at the next start."""
        boot = self.boot_dir()
        if not boot:
            return
        path = os.path.join(boot, TZ_FILE)
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            return
        raw = read_zone_file(text)
        if raw.lower() in TZ_AUTO:
            self.store_zone(None)
            result = "applied: time zone from the location"
        elif valid_zone(raw):
            self.store_zone(raw)
            result = f"applied: {raw}"
        else:
            result = f"not applied: {raw[:64]!r} is not a time zone name such as Asia/Kolkata"
        log(f"{TZ_FILE}: {result}")
        try:
            Path(f"{path}.applied").write_text(f"{text.rstrip()}\n# {result}\n")
            os.unlink(path)
        except OSError as e:
            log(f"{TZ_FILE} could not be renamed: {e}")

    def wanted_zone(self) -> tuple[str | None, str]:
        """(zone, where it comes from): "setting" (explicit), "location", or (None, "none")."""
        z = self.explicit_zone()
        if z:
            return z, "setting"
        try:
            s = self.settings()
        except ApiError:
            s = None
        z = self.located_zone(s)
        return (z, "location") if z else (None, "none")

    def use_zone_in_process(self, zone: str) -> None:
        # This process keeps the zone it started with; dashboard times of the older BirdNET-Go layout need the new one.
        os.environ["TZ"] = zone
        time.tzset()

    def birdnet_unit_text(self) -> tuple[str | None, str | None]:
        """(text, path it came from) of BirdNET-Go's unit, or (None, None) when BirdNET-Go is not installed."""
        for path in (self.cfg.birdnet_unit, self.cfg.birdnet_unit_lib):
            try:
                return Path(path).read_text(), path
            except OSError:
                continue
        return None, None

    def mark_notes_boundary(self, old_zone: str) -> None:
        """Older BirdNET-Go (`notes` table) labels rows in its container's zone. Tell wdx-agent which rows were
        written in the old zone, so ones not sent yet keep their true time (wdx-agent 0.4.6 notes_zone_before)."""
        try:
            s = self.settings()
            db = open_ro(self.db_path(s))
            try:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                layout = getattr(self.agent, "birdnet_go_layout", lambda t: "notes" if "notes" in t else None)(tables)
                last = db.execute("SELECT MAX(id) FROM notes").fetchone()[0] if layout == "notes" else None
            finally:
                db.close()
        except (ApiError, sqlite3.Error, OSError):
            return
        if not last:
            return
        before = str(self.agent_ini().get("notes_zone_before", "") or "").strip()
        value = ", ".join(v for v in (before, f"{int(last)} {old_zone}") if v)
        with self.lock:
            write_ini(self.cfg.agent_config, "agent", {"notes_zone_before": value})
        log(f"BirdNET-Go notes up to id {last} were written in {old_zone} (notes_zone_before)")
        if self.agent_older_than(str(getattr(self.agent, "VERSION", "0")), "0.4.6"):
            log("WARNING: wdx-agent older than 0.4.6 ignores notes_zone_before: detections not sent yet may be "
                "shifted by the time zone change; update it (run install.sh again)")

    def set_birdnet_zone(self, zone: str) -> tuple[bool, list[str]]:
        """TZ of BirdNET-Go's container: rewrite its unit and restart it. (restarted, warnings)"""
        text, src = self.birdnet_unit_text()
        if text is None:
            return False, []
        old = unit_zone(text)
        if old == zone:
            return False, []
        if old is None:
            return False, [f"BirdNET-Go's service ({src}) sets no TZ; its times stay in UTC"]
        svc = self.cfg.birdnet_service
        self.run(["systemctl", "stop", svc], 120)   # no rows written in the old zone after the boundary below
        self.mark_notes_boundary(old)
        warnings = []
        try:
            write_atomic(self.cfg.birdnet_unit, set_unit_zone(text, zone).encode(), 0o644)
        except OSError as e:
            warnings.append(f"BirdNET-Go's service not changed: {e}")
        self.run(["systemctl", "daemon-reload"], 60)
        rc, out = self.run(["systemctl", "start", svc], 120)
        if rc != 0:
            warnings.append(f"BirdNET-Go not restarted: {out.strip()[:200] or 'systemctl not available'}")
        if not warnings:
            log(f"BirdNET-Go time zone {old} -> {zone} ({self.cfg.birdnet_unit})")
        return rc == 0, warnings

    def apply_timezone(self, reason: str) -> dict:
        """Put the host and BirdNET-Go on the wanted zone. Does nothing when both are on it already."""
        with self.tz_lock:
            zone, source = self.wanted_zone()
            res = {"zone": zone, "source": source, "changed": False, "birdnetRestarted": False, "warnings": []}
            if not zone:
                return res
            current = self.system_zone()
            if current != zone:
                rc, out = self.run(["timedatectl", "set-timezone", zone], 30)
                if rc != 0:
                    res["warnings"].append(f"time zone not set to {zone}: {out.strip()[:200] or 'timedatectl not available'}")
                else:
                    log(f"time zone {current or 'unknown'} -> {zone} (from {source}, {reason})")
                    res["changed"] = True
                    if os.path.isfile(self.cfg.etc_timezone):   # Debian's older copy of the zone name
                        try:
                            write_atomic(self.cfg.etc_timezone, f"{zone}\n".encode(), 0o644)
                        except OSError:
                            pass
                    self.use_zone_in_process(zone)
            restarted, warnings = self.set_birdnet_zone(zone)
            res["birdnetRestarted"] = restarted
            res["changed"] = res["changed"] or restarted
            res["warnings"] += warnings
            if res["changed"]:
                # wdx-agent reads BirdNET-Go's older layout in local time: it must run in the new zone too
                self.run(["systemctl", "restart", self.cfg.agent_service], 60)
            return res

    def clips_dir(self, s) -> str:
        return self.cfg.clips_dir or os.path.join(os.path.dirname(self.db_path(s)), "clips")

    def check_code(self, ip: str, given: str) -> None:
        now = time.monotonic()
        with self.lock:
            q = self.fails[ip]
            while q and now - q[0] > FAIL_WINDOW:
                q.popleft()
            if len(q) >= FAIL_LIMIT:
                raise ApiError(429, "too many wrong setup codes; wait 10 minutes")
            if hmac.compare_digest(given.strip().upper().encode(), self.setup_code.encode()):
                return
            q.append(now)
        raise ApiError(403, "wrong setup code")

    @staticmethod
    def configured(s) -> bool:
        # Set up = the Base knows where it is. The key comes by itself (registration) or from the field app.
        return bool(s and s.latitude is not None and s.longitude is not None)

    def birdnet_go_version(self) -> str | None:
        if self._bng and time.monotonic() - self._bng[0] < 600:
            return self._bng[1]
        version = None
        rc, out = self.run(["docker", "inspect", "--format", "{{.Config.Image}}", "birdnet-go"], 10)
        if rc == 0 and out.strip():
            image = out.strip().splitlines()[0]
            version = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else image
        self._bng = (time.monotonic(), version)
        return version

    def birdnet_audio_log(self) -> bool | None:
        """From BirdNET-Go's recent log: True when capture started last, False when it last said it has no
        audio source, None when the log says neither (or docker is not available)."""
        name = self.cfg.birdnet_container
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            return None
        rc, out = self.run(["sh", "-c", f"docker logs --tail 500 {name} 2>&1"], 15)
        if rc != 0:
            return None
        low = out.lower()
        bad = max(low.rfind(p) for p in MIC_LOG_BAD)
        ok = max(low.rfind(p) for p in MIC_LOG_OK)
        if bad < 0 and ok < 0:
            return None
        return ok > bad

    def microphone(self) -> dict:
        """Whether BirdNET-Go has a microphone. `found` is true, false, or null when it cannot be told.

        Signals, strongest first: BirdNET-Go's own log (no active audio source / capture started), then the capture
        devices in `arecord -l` against the device set in BirdNET-Go's config.yaml."""
        if self._mic and time.monotonic() - self._mic[0] < MIC_CHECK_EVERY:
            return self._mic[1]
        res = {"found": None, "device": None, "devices": [], "problem": None}
        if mic is not None:
            devices = mic.list_devices(self.run)
            try:
                text = Path(self.birdnet_config(self.settings())).read_text("utf-8", "surrogateescape")
                res["device"] = mic.read_device(text)
            except (ApiError, OSError):
                pass
            res["devices"] = [mic.describe(d) for d in devices or []]
            heard = self.birdnet_audio_log()
            if heard is False:
                res["found"], res["problem"] = False, "BirdNET-Go is running without an active audio source."
            elif devices == []:
                res["found"], res["problem"] = False, "No sound card that can record is connected."
            elif heard is True:
                res["found"] = True
            elif devices:
                res["found"] = mic.device_ok(res["device"], devices)
                if not res["found"]:
                    res["problem"] = f'BirdNET-Go is set to record from "{res["device"] or "sysdefault"}", which cannot record.'
        self._mic = (time.monotonic(), res)
        return res

    # --- endpoints ---------------------------------------------------------------------------------------

    def info(self) -> dict:
        try:
            s = self.settings()
        except ApiError:
            s = None
        return {
            "hardwareId": self.hw, "model": "wildnetwork-base", "hostname": socket.gethostname(),
            "software": {"wnbase": VERSION, "wdx-agent": getattr(self.agent, "VERSION", None),
                         "birdnetGo": self.birdnet_go_version()},
            "configured": self.configured(s),
            "deviceId": (s.device_id or None) if s else None,
            # The id Wildlife Network files this Base's detections under (GET /api/v1/events?deployment=...), worked out
            # by wdx-agent's own settings so it always matches what the agent sends.
            "deploymentId": f"{s.system}:{s.station_id}" if s and getattr(s, "station_id", "") else None,
            "name": (s.station_name or None) if s else None,
            "latitude": s.latitude if s else None,
            "longitude": s.longitude if s else None,
            "registration": "registered" if s and s.api_key else self.registration,
            "microphone": self.microphone(),
            "test": self.test_mode(),
            "uplink": self.uplink(),
            **self.timezone_info(),
            "update": self.update_info(),
            "alerts": self.alerts_info(),
        }

    # --- pausing Wildlife Network's alert emails about this Base ------------------------------------------

    def alerts_path(self) -> str:
        return os.path.join(self.cfg.state_dir, "alerts.json")

    @staticmethod
    def parse_pause(v) -> str | None:
        """alertsPausedUntil: an ISO time with a zone (normalised to UTC, whole seconds) or None (alerts back on)."""
        if v is None:
            return None
        if not isinstance(v, str) or len(v) > 40:
            raise ApiError(400, "invalid alertsPausedUntil: an ISO time such as 2026-12-01T00:00:00Z, or null")
        try:
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ApiError(400, "invalid alertsPausedUntil: an ISO time such as 2026-12-01T00:00:00Z, or null")
        if dt.tzinfo is None:
            raise ApiError(400, "invalid alertsPausedUntil: give the time zone, for example a Z at the end")
        dt = dt.astimezone(timezone.utc)
        if dt.timestamp() <= time.time():
            raise ApiError(400, "alertsPausedUntil is in the past; send null to turn alerts back on")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def pause_alerts(self, until: str | None) -> None:
        """Keep the choice locally (shown in the dashboard) and queue it for Wildlife Network."""
        with self.lock:
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            write_atomic(self.alerts_path(), json.dumps({"alertsPausedUntil": until, "changedAt": iso_utc(time.time())}).encode(),
                         0o644)
        self.queue_settings({"alertsPausedUntil": until})
        log(f"alert emails {'paused until ' + until if until else 'back on'}")
        self.reg_wake.set()   # send it now rather than at the next registration check

    def alerts_info(self) -> dict:
        """What was last chosen; `sent` is false while it still waits in the queue for Wildlife Network."""
        try:
            st = json.loads(Path(self.alerts_path()).read_text())
        except (OSError, ValueError):
            st = {}
        until = st.get("alertsPausedUntil")
        paused = False
        if isinstance(until, str):
            try:
                paused = datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp() > time.time()
            except ValueError:
                until = None
        try:
            pending = "alertsPausedUntil" in json.loads(Path(self.pending_settings_path()).read_text())
        except (OSError, ValueError):
            pending = False
        return {"paused": paused, "pausedUntil": until if paused else None,
                "forever": paused and until >= ALERTS_FOREVER[:4], "sent": not pending}

    # --- automatic updates (wnupdate.py, run by wnbase-update.service) --------------------------------------

    def auto_update(self) -> bool:
        """[base] auto_update in wnbase.ini (read each time: Setup and wnupdate.py share the file)."""
        raw = self.cfg.auto_update
        if self.cfg.path:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(self.cfg.path)
            raw = cp.get("base", "auto_update", fallback="")
        return wnupdate.switch_on(raw) if wnupdate else str(raw).strip().lower() not in ("0", "no", "false", "off")

    def store_auto_update(self, on: bool) -> None:
        self.cfg.auto_update = "yes" if on else "no"
        if self.cfg.path:
            with self.lock:
                write_ini(self.cfg.path, "base", {"auto_update": self.cfg.auto_update})
        log(f"automatic updates {'on' if on else 'off'}")

    def update_info(self) -> dict:
        st = wnupdate.read_state(self.cfg.state_dir) if wnupdate else {}
        return {"auto": self.auto_update(),
                "current": {"wnbase": VERSION, "wdx-agent": getattr(self.agent, "VERSION", None)},
                "latest": st.get("latest") or None, "lastCheck": st.get("lastCheck"), "lastResult": st.get("lastResult")}

    def start_timezone(self) -> threading.Thread:
        """At every start: a Base that knows its location (or has a zone set) but runs on another zone, such as one
        set up before this version or reset by a reinstall, switches once. In the background: BirdNET-Go restarts."""
        def run():
            try:
                res = self.apply_timezone("wnbase start")
                for w in res["warnings"]:
                    log(f"time zone: {w}")
            except Exception:  # noqa: BLE001 - never stop wnbase over this
                log(f"time zone check failed:\n{traceback.format_exc()}")
        t = threading.Thread(target=run, name="timezone", daemon=True)
        t.start()
        return t

    def timezone_info(self) -> dict:
        """Active time zone of the Base, where it comes from, and BirdNET-Go's (they differ only until it restarts)."""
        active = self.system_zone()
        wanted, source = self.wanted_zone()
        text, _ = self.birdnet_unit_text()
        return {"timezone": active, "timezoneSource": source if wanted and wanted == active else "system",
                "timezoneSetting": self.explicit_zone(), "birdnetGoTimezone": unit_zone(text) if text else None}

    # --- registration with Wildlife Network -------------------------------------------------------------------

    def agent_ini(self) -> configparser.SectionProxy | dict:
        """The [agent] section as written (read directly, so keys an older wdx-agent ignores are seen too)."""
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(self.cfg.agent_config)
        return cp["agent"] if cp.has_section("agent") else {}

    def test_mode(self) -> bool:
        return str(self.agent_ini().get("test", "")).strip().lower() in ("1", "yes", "true", "on")

    def api_base(self, s) -> str:
        return s.endpoint.split("/api/", 1)[0]

    def pending_settings_path(self) -> str:
        return os.path.join(self.cfg.state_dir, "pending-settings.json")

    def sync_settings(self) -> bool:
        """Send device settings waiting in the queue (test mode) to Wildlife Network. True when none are left.

        POST <site>/api/v1/devices/<deviceId>/settings with the device key. Kept on disk until it is accepted,
        so a Base that is offline when the owner changes it sends it later."""
        path = self.pending_settings_path()
        try:
            pending = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return True
        try:
            s = self.settings()
        except ApiError:
            return False
        if not s.api_key or not s.device_id:
            return False   # not registered yet: sent once it is
        url = f"{self.api_base(s)}/api/v1/devices/{quote(s.device_id, safe='')}/settings"
        try:
            status, res = self.http_post(url, pending, 30, token=s.api_key)
        except (OSError, ValueError) as e:
            log(f"device settings not sent yet ({type(e).__name__}); will retry")
            return False
        if status in (200, 201, 204):
            with self.lock:
                try:
                    if json.loads(Path(path).read_text()) == pending:   # not changed again meanwhile
                        os.unlink(path)
                except (OSError, ValueError):
                    pass
            log(f"device settings sent to Wildlife Network: {json.dumps(pending)}")
            return True
        log(f"device settings refused (HTTP {status}: {str(res.get('error') or '')[:120]}); will retry")
        return False

    def queue_settings(self, values: dict) -> None:
        path = self.pending_settings_path()
        with self.lock:
            try:
                pending = json.loads(Path(path).read_text())
            except (OSError, ValueError):
                pending = {}
            pending.update(values)
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            write_atomic(path, json.dumps(pending).encode(), 0o600)

    # --- health reports to Wildlife Network ---------------------------------------------------------------

    def boots(self) -> int | None:
        """How many times this Base has started, counted by wnbase from the kernel's boot id."""
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            return None
        path = os.path.join(self.cfg.state_dir, "boots.json")
        with self.lock:
            try:
                st = json.loads(Path(path).read_text())
            except (OSError, ValueError):
                st = {"count": 0, "bootId": None}
            if st.get("bootId") != boot_id:
                st = {"count": int(st.get("count") or 0) + 1, "bootId": boot_id}
                try:
                    os.makedirs(self.cfg.state_dir, exist_ok=True)
                    write_atomic(path, json.dumps(st).encode(), 0o644)
                except OSError:
                    pass
        return st["count"]

    def wifi_signal(self) -> int | None:
        rc, out = self.run(["nmcli", "-t", "-f", "ACTIVE,SIGNAL", "device", "wifi", "list", "--rescan", "no"], 15)
        if rc == 0:
            for line in out.splitlines():
                f = nmcli_fields(line)
                if len(f) >= 2 and f[0] == "yes" and f[1].isdigit():
                    return int(f[1])
        return None

    def health(self) -> dict:
        """One health report: versions, uptime, boots, temperature, storage, microphone, queue and network.
        No audio, no detections, no location."""
        rec: dict = {"at": iso_utc(time.time()),
                     "software": {"wnbase": VERSION, "wdx-agent": getattr(self.agent, "VERSION", None),
                                  "birdnet-go": self.birdnet_go_version()}}
        st: dict = {}
        try:
            s = self.settings()
            st = self.agent.collect_status(s)
        except (ApiError, SystemExit, OSError, sqlite3.Error):
            s = None
        try:
            rec["uptimeSeconds"] = int(float(Path("/proc/uptime").read_text().split()[0]))
        except (OSError, ValueError, IndexError):
            pass
        boots = self.boots()
        if boots is not None:
            rec["boots"] = boots
        for key in ("temperatureC", "storage", "queue"):
            if key in st:
                rec[key] = st[key]
        last = None
        if s is not None:
            try:
                rows = self.recent(168)["detections"]
                last = rows[0]["time"] if rows else None
            except ApiError:
                pass
        rec["microphone"] = {"active": self.microphone().get("found") is True, "lastDetectionAt": last}
        net = self.network()
        kind = net.get("uplink")
        if kind:
            n: dict = {"type": kind}
            sig = (net.get("cellular") or {}).get("signalPercent") if kind == "4g" else \
                self.wifi_signal() if kind == "wifi" else None
            if sig is not None:
                n["signal"], n["signalUnit"] = sig, "percent"
            rec["network"] = n
        return rec

    def health_queue_path(self) -> str:
        return os.path.join(self.cfg.state_dir, "health-queue.json")

    def send_health(self, rec: dict | None = None) -> bool:
        """Queue a report (a new one unless given) and send everything queued. True when the queue is empty.

        POST <site>/api/v1/devices/<deviceId>/health with the device key: one object, or an array of them when
        older reports were waiting. At most HEALTH_QUEUE_MAX are kept; the oldest go first."""
        rec = rec if rec is not None else self.health()
        path = self.health_queue_path()
        with self.lock:
            try:
                queue = json.loads(Path(path).read_text())
                queue = queue if isinstance(queue, list) else []
            except (OSError, ValueError):
                queue = []
            queue = (queue + [rec])[-HEALTH_QUEUE_MAX:]
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            write_atomic(path, json.dumps(queue).encode(), 0o600)
        try:
            s = self.settings()
        except ApiError:
            return False
        if not s.api_key or not s.device_id:
            return False
        url = f"{self.api_base(s)}/api/v1/devices/{quote(s.device_id, safe='')}/health"
        try:
            status, res = self.http_post(url, queue if len(queue) > 1 else queue[0], 30, token=s.api_key)
        except (OSError, ValueError) as e:
            log(f"health report kept for later ({len(queue)} waiting; {type(e).__name__})")
            return False
        if status not in (200, 201, 202, 204):
            log(f"health report refused (HTTP {status}: {str(res.get('error') or '')[:120]}); {len(queue)} waiting")
            return False
        with self.lock:
            try:
                now = json.loads(Path(path).read_text())
                rest = now[len(queue):] if isinstance(now, list) and now[:len(queue)] == queue else []
                write_atomic(path, json.dumps(rest).encode(), 0o600)
            except (OSError, ValueError):
                pass
        return True

    def start_health(self) -> None:
        def loop():
            time.sleep(60)   # let BirdNET-Go and the network come up first
            while True:
                try:
                    self.send_health()
                except Exception:
                    log(f"health report error:\n{traceback.format_exc()}")
                time.sleep(HEALTH_EVERY)
        threading.Thread(target=loop, name="health", daemon=True).start()

    def mdns_txt(self) -> dict[str, str]:
        name = str(self.agent_ini().get("station_name", "") or "").strip() or socket.gethostname()
        # TXT strings are at most 255 bytes each; names are at most 64 characters here
        return {"name": name[:64], "model": "wildnetwork-base", "version": VERSION, "hw": self.hw,
                "path": "/api/info", "test": "yes" if self.test_mode() else "no"}

    def write_mdns(self) -> None:
        """Advertise wnbase over mDNS/DNS-SD (avahi picks up a changed file by itself)."""
        path = self.cfg.avahi_service_file
        if not path or not os.path.isdir(os.path.dirname(path)):
            return
        data = avahi_service_xml(self.cfg.port, self.mdns_txt()).encode()
        try:
            if Path(path).read_bytes() == data:
                return
        except OSError:
            pass
        try:
            write_atomic(path, data, 0o644)
            log(f"mDNS: advertising {MDNS_TYPE} on port {self.cfg.port}")
        except OSError as e:
            log(f"mDNS service file not written: {e}")

    def register_once(self) -> int:
        """One registration attempt when the Base is set up but has no key. Returns seconds until the next check."""
        try:
            s = self.settings()
        except ApiError:
            self.registration = "error: wdx-agent config missing"
            return REGISTER_EVERY
        if s.api_key:  # registered earlier, or the field app wrote a key: nothing to do
            self.registration, self.reg_backoff = "registered", REGISTER_EVERY
            return REGISTER_EVERY
        if len(s.station_name.strip()) < 3 or s.latitude is None or s.longitude is None:
            self.registration, self.reg_backoff = "waiting-for-setup", REGISTER_EVERY
            return REGISTER_EVERY
        url = s.endpoint.rsplit("/events", 1)[0] + "/devices"
        body = {"name": s.station_name.strip(), "model": "wildnetwork-base", "hardwareId": self.hw}
        try:
            status, res = self.http_post(url, body, 30)
        except (OSError, ValueError) as e:  # URLError, timeouts, DNS: most likely no internet yet
            self.registration = "waiting-for-internet"
            log(f"registration: Wildlife Network not reachable ({type(e).__name__}); retrying in {self.reg_backoff}s")
            return self._backoff()
        if status == 429:
            self.registration = "error: too many new devices from this network today"
            log("registration: Wildlife Network allows 5 new devices per network per day; retrying in an hour")
            return REGISTER_MAX_BACKOFF
        key, device = res.get("apiKey"), res.get("deviceId")
        if status not in (200, 201) or not isinstance(key, str) or not key or not isinstance(device, str):
            reason = str(res.get("error") or f"HTTP {status}")[:80]
            self.registration = f"error: {reason}"
            log(f"registration refused: {reason}; retrying in {self.reg_backoff}s")
            return self._backoff()
        with self.lock:
            if self.settings().api_key:  # a key arrived from the field app meanwhile: keep it
                self.registration = "registered"
                return REGISTER_EVERY
            write_ini(self.cfg.agent_config, "agent", {"api_key": key, "device_id": device, "system": "wildnetwork-base"})
        log(f"registered with Wildlife Network as device {device}")
        rc, out = self.run(["systemctl", "restart", self.cfg.agent_service], 60)
        if rc != 0:
            log(f"could not restart {self.cfg.agent_service}: {out.strip()[:200]}")
        self.registration, self.reg_backoff = "registered", REGISTER_EVERY
        return REGISTER_EVERY

    def _backoff(self) -> int:
        wait = self.reg_backoff
        self.reg_backoff = min(self.reg_backoff * 2, REGISTER_MAX_BACKOFF)
        return wait

    def start_registration(self) -> None:
        def loop():
            while True:
                try:
                    wait = self.register_once()
                except Exception:
                    log(f"registration error:\n{traceback.format_exc()}")
                    wait = REGISTER_MAX_BACKOFF
                try:
                    if not self.sync_settings():
                        wait = min(wait, 120)
                except Exception:
                    log(f"device settings error:\n{traceback.format_exc()}")
                self.reg_wake.wait(wait)
                self.reg_wake.clear()
        threading.Thread(target=loop, name="registration", daemon=True).start()

    def configure(self, body: dict) -> dict:
        allowed = {"name", "latitude", "longitude", "roundCoords", "apiKey", "deviceId", "endpoint", "apn", "wifi", "test",
                   "timezone", "autoUpdate", "alertsPausedUntil"}
        unknown = set(body) - allowed
        if unknown:
            raise ApiError(400, f"unknown field: {sorted(unknown)[0]}")
        updates: dict[str, str] = {}

        def text(key, pattern, maxlen):
            v = body[key]
            if not isinstance(v, str) or len(v) > maxlen or not re.fullmatch(pattern, v):
                raise ApiError(400, f"invalid {key}")
            return v

        if "name" in body:
            updates["station_name"] = text("name", r"[^\x00-\x1f\x7f]*", 64).strip()
        lat_lon: dict[str, float] = {}
        for key, lim in (("latitude", 90), ("longitude", 180)):
            if key in body:
                v = body[key]
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    raise ApiError(400, f"invalid {key}")
                if isinstance(v, bool) or not -lim <= f <= lim:
                    raise ApiError(400, f"invalid {key}")
                updates[key] = repr(f)
                lat_lon[key] = f
        if "roundCoords" in body:
            v = body["roundCoords"]
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 5:
                raise ApiError(400, "invalid roundCoords")
            updates["round_coords"] = str(v)
        if "apiKey" in body:
            updates["api_key"] = text("apiKey", r"[\x21-\x7e]*", 200)
        if "deviceId" in body:
            updates["device_id"] = text("deviceId", r"[A-Za-z0-9_.:-]*", 100)
            # A key from the Wildlife Network device registry only accepts events from this system name.
            updates["system"] = "wildnetwork-base"
            updates["sensor_model"] = "Wildlife Network Base"
        if "endpoint" in body:
            updates["endpoint"] = text("endpoint", r"https?://[\x21-\x7e]+", 300)
        test = None
        if "test" in body:
            if not isinstance(body["test"], bool):
                raise ApiError(400, "invalid test: true or false")
            test = body["test"]
            if test != self.test_mode():
                updates["test"] = "yes" if test else "no"
        apn = text("apn", r"[A-Za-z0-9._-]{1,63}", 63) if "apn" in body else None
        pause = self.parse_pause(body["alertsPausedUntil"]) if "alertsPausedUntil" in body else False
        if "autoUpdate" in body and not isinstance(body["autoUpdate"], bool):
            raise ApiError(400, "invalid autoUpdate: true or false")
        zone = False   # False: not given; None: back to the location's zone
        if "timezone" in body:
            v = body["timezone"]
            if v is None or (isinstance(v, str) and v.strip().lower() in TZ_AUTO):
                zone = None
            elif not valid_zone(v):
                raise ApiError(400, "invalid timezone: an IANA name such as Asia/Kolkata, or empty for the location's")
            else:
                zone = v.strip()
        wifi = body.get("wifi")
        if wifi is not None:
            if not isinstance(wifi, dict) or not isinstance(wifi.get("ssid"), str) or not isinstance(wifi.get("psk"), str):
                raise ApiError(400, "invalid wifi")
            if not 1 <= len(wifi["ssid"].encode()) <= 32 or not re.fullmatch(r"[\x20-\x7e]{8,63}", wifi["psk"]):
                raise ApiError(400, "invalid wifi: ssid 1 to 32 bytes, psk 8 to 63 characters")

        warnings: list[str] = []
        if pause is not False:
            self.pause_alerts(pause)
        if "autoUpdate" in body and body["autoUpdate"] != self.auto_update():
            self.store_auto_update(body["autoUpdate"])
        if updates:
            with self.lock:
                write_ini(self.cfg.agent_config, "agent", updates)
            log(f"config updated: {', '.join(sorted(updates))}")
        if apn is not None:
            warnings += self.set_apn(apn)
        if wifi is not None:
            warnings += self.set_wifi(wifi["ssid"], wifi["psk"])
        if "station_name" in updates or "test" in updates:
            self.write_mdns()
        if "test" in updates:
            # The server keeps a test station apart too (also for events an older wdx-agent sends without the
            # flag); queued, so it is sent when the Base is online and registered.
            self.queue_settings({"test": test})
            log(f"test station {'on' if test else 'off'}")
            agent_version = str(getattr(self.agent, "VERSION", "0"))
            if test and self.agent_older_than(agent_version, "0.4.4"):
                warnings.append(f"wdx-agent {agent_version} does not mark events as test; update it (run install.sh again). "
                                "Wildlife Network is told about the test station separately.")
        notes: list[str] = []
        tz = None
        if zone is not False:
            self.store_zone(zone)
        if "latitude" in updates or "longitude" in updates or zone is not False:
            located = "latitude" in updates or "longitude" in updates
            if located:
                notes = self.set_birdnet_location(lat_lon.get("latitude"), lat_lon.get("longitude"), restart=False)
            else:
                self.birdnet_location_changed = False
            tz = self.apply_timezone("location saved" if located else "time zone set")
            warnings += tz["warnings"]
            if self.birdnet_location_changed and not tz["birdnetRestarted"]:
                notes += self.restart_birdnet()
        if updates:
            rc, out = self.run(["systemctl", "restart", self.cfg.agent_service], 60)
            if rc != 0:
                warnings.append(f"could not restart {self.cfg.agent_service}: {out.strip()[:200]}")
            self.reg_backoff = REGISTER_EVERY
            self.reg_wake.set()  # try to register right away with the new name and location
        try:
            configured = self.configured(self.settings())
        except ApiError:
            configured = False
        res = {"ok": True, "configured": configured}
        if tz and tz["zone"]:
            res["timezone"] = tz["zone"]
        if warnings:
            res["warnings"] = warnings
        if notes:
            res["notes"] = notes
        return res

    @staticmethod
    def agent_older_than(version: str, wanted: str) -> bool:
        def parts(v):
            return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))
        return parts(version) < parts(wanted)

    def birdnet_password(self) -> dict:
        """BirdNET-Go's settings password made by this Base (bngauth.py), for the owner who has the setup code."""
        try:
            pw = Path(self.cfg.birdnet_password_file).read_text().strip()
        except OSError:
            pw = ""
        if not pw:
            raise ApiError(404, "no BirdNET-Go password made by this Base (it may have been set in BirdNET-Go)")
        return {"password": pw, "url": ":8080"}

    def connection_exists(self, name: str) -> bool:
        rc, out = self.run(["nmcli", "-t", "-f", "NAME", "connection", "show"], 15)
        return rc == 0 and name in [nmcli_fields(x)[0] for x in out.splitlines()]

    def set_apn(self, apn: str) -> list[str]:
        if self.connection_exists("wn-4g"):
            args = ["nmcli", "connection", "modify", "wn-4g", "gsm.apn", apn]
        else:
            args = ["nmcli", "connection", "add", "type", "gsm", "ifname", "*", "con-name", "wn-4g",
                    "gsm.apn", apn, "connection.autoconnect", "yes"]
        rc, out = self.run(args, 30)
        if rc != 0:
            return [f"4G APN not saved: {out.strip()[:200] or 'nmcli not available'}"]
        log(f"4G connection wn-4g uses APN {apn}")
        self.run(["nmcli", "connection", "up", "wn-4g"], 60)  # best effort: no modem or no SIM is fine for now
        return []

    def set_wifi(self, ssid: str, psk: str) -> list[str]:
        """Save home Wi-Fi in the one managed profile (wn-uplink) and switch off other Wi-Fi client profiles.

        Not brought up now: the radio may be serving this hotspot to the phone doing the setup. wn-net.service
        moves to it once no phone is on the hotspot (within 5 minutes), or at the next boot."""
        import wnnet
        return wnnet.save_uplink(self.run, self.cfg.hotspot_ifname, ssid, psk)

    def uplink(self) -> dict | None:
        """The connection carrying traffic out: {type, profile, ssid (Wi-Fi only)}, or None."""
        net = self.network()
        kinds = {"gsm": "4g", "802-3-ethernet": "ethernet", "802-11-wireless": "wifi"}
        for a in net.get("active") or []:
            if a["type"] in kinds and a["name"] != "wn-hotspot":
                up = {"type": kinds[a["type"]], "profile": a["name"], "ssid": None}
                if up["type"] == "wifi":
                    rc, out = self.run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", a["name"]], 15)
                    up["ssid"] = (out.strip() or None) if rc == 0 else None
                return up
        return None

    def network(self) -> dict:
        net: dict = {}
        rc, out = self.run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"], 15)
        if rc == 0:
            active = []
            for line in out.splitlines():
                parts = nmcli_fields(line)
                if len(parts) >= 3 and parts[1] != "loopback":
                    active.append({"name": parts[0], "type": parts[1], "device": parts[2]})
            net["active"] = active
            kinds = {"gsm": "4g", "802-3-ethernet": "ethernet", "802-11-wireless": "wifi"}
            uplinks = [kinds[a["type"]] for a in active if a["type"] in kinds and a["name"] != "wn-hotspot"]
            net["uplink"] = uplinks[0] if uplinks else None
            net["hotspot"] = any(a["name"] == "wn-hotspot" for a in active)
        rc, out = self.run(["mmcli", "-m", "any", "-K"], 15)
        if rc == 0:
            kv = {}
            for line in out.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    kv[k.strip()] = v.strip()
            cell: dict = {}
            try:
                cell["signalPercent"] = int(kv.get("modem.generic.signal-quality.value", ""))
            except ValueError:
                pass
            for key, name in (("modem.generic.access-technologies.value[1]", "accessTechnology"),
                              ("modem.3gpp.operator-name", "operator"), ("modem.generic.state", "state")):
                if kv.get(key) and kv[key] != "--":
                    cell[name] = kv[key]
            net["cellular"] = cell
        return net

    def status(self) -> dict:
        s = self.settings()
        try:
            st = self.agent.collect_status(s)
        except SystemExit as e:  # wdx-agent exits when the database has no coordinates and none are configured
            st = {"error": str(e)}
        if isinstance(st.get("software"), dict):
            st["software"]["wnbase"] = VERSION
        try:
            st["lastUpload"] = iso_utc(os.path.getmtime(s.state_path))
        except OSError:
            st["lastUpload"] = None
        st["network"] = self.network()
        return st

    def detections(self, limit: int) -> dict:
        s = self.settings()
        try:
            batch = self.agent.pending_batch(s)
        except SystemExit as e:
            raise ApiError(503, str(e))
        except sqlite3.Error as e:
            raise ApiError(503, f"database: {e}")
        events, cursor = [], None
        for c, e in batch:
            if e is not None:
                if len(events) >= limit:
                    break
                events.append(e)
            cursor = c
        with self.lock:
            self.served_cursor = canon(cursor) if cursor is not None else None
        return {"cursor": cursor, "events": events}

    def ack(self, body: dict) -> dict:
        if "cursor" not in body or body["cursor"] is None:
            raise ApiError(400, "cursor is required")
        cursor = body["cursor"]
        with self.lock:
            if self.served_cursor is None or canon(cursor) != self.served_cursor:
                raise ApiError(409, "cursor does not match the last /api/detections response")
            s = self.settings()
            current = self.agent.load_state(s).get(self.agent.state_key(s))
            a, b = cursor_rank(current), cursor_rank(cursor)
            # The uploader may have moved on while the phone was busy; never move the cursor backwards.
            advanced = not (a and b and a[0] == b[0] and a[1] >= b[1])
            if advanced:
                self.agent.save_cursor(s, cursor)
            self.served_cursor = None
        log(f"pickup acknowledged up to {canon(cursor)}" + ("" if advanced else " (uploader was already past it)"))
        return {"ok": True, "advanced": advanced}

    def recent(self, hours: int) -> dict:
        s = self.settings()
        path = self.db_path(s)
        cutoff = time.time() - hours * 3600
        rows = []
        try:
            db = open_ro(path)
            try:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if "detections" in tables and "labels" in tables:
                    lt = "label_types" in tables
                    q = ("SELECT d.id, d.detected_at, l.scientific_name, d.confidence, d.clip_name, COALESCE(d.unlikely, 0) "
                         "FROM detections d JOIN labels l ON l.id = d.label_id " +
                         ("LEFT JOIN label_types lt ON lt.id = l.label_type_id " if lt else "") +
                         "WHERE d.detected_at >= ? " + ("AND COALESCE(lt.name, 'species') = 'species' " if lt else "") +
                         "ORDER BY d.detected_at DESC, d.id DESC LIMIT ?")
                    for rid, at, sci, conf, clip, unlikely in db.execute(q, (int(cutoff), RECENT_MAX)):
                        rows.append({"id": rid, "time": iso_utc(int(at)), "scientificName": sci, "commonName": None,
                                     "confidence": conf, "clip": os.path.basename(clip) if clip else None,
                                     "unlikely": bool(unlikely)})
                elif "notes" in tables:
                    local = datetime.fromtimestamp(cutoff)
                    d, t = local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")
                    q = ("SELECT id, date, time, scientific_name, common_name, confidence, clip_name FROM notes "
                         "WHERE date > ? OR (date = ? AND time >= ?) ORDER BY date DESC, time DESC, id DESC LIMIT ?")
                    notes_iso = getattr(self.agent, "notes_iso", None)   # wdx-agent 0.4.6: rows from before a zone change
                    for rid, date, clock, sci, com, conf, clip in db.execute(q, (d, d, t, RECENT_MAX)):
                        when = notes_iso(s, rid, date, clock) if notes_iso else self.agent.local_iso(date, clock)
                        rows.append({"id": rid, "time": when, "scientificName": sci,
                                     "commonName": com, "confidence": conf,
                                     "clip": os.path.basename(clip) if clip else None, "unlikely": False})
            finally:
                db.close()
        except sqlite3.Error as e:
            raise ApiError(503, f"database: {e}")
        return {"hours": hours, "detections": rows}

    def clip_path(self, raw: str) -> str:
        name = unquote(raw)
        if (not name or "/" in name or "\\" in name or ".." in name or "\x00" in name or name.startswith(".")
                or os.path.normpath(name) != name):
            raise ApiError(400, "invalid clip name")
        if os.path.splitext(name)[1].lower() not in AUDIO_TYPES:
            raise ApiError(404, "not found")
        s = self.settings()
        base = os.path.realpath(self.clips_dir(s))
        candidates = [name]
        # BirdNET-Go keeps clips in dated folders; the database knows the folder for a file name.
        try:
            db = open_ro(self.db_path(s))
            try:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                table = "detections" if "detections" in tables else "notes" if "notes" in tables else None
                if table:
                    like = "%/" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    for (clip,) in db.execute(f"SELECT clip_name FROM {table} WHERE clip_name = ? OR clip_name LIKE ? "
                                              "ESCAPE '\\' ORDER BY id DESC LIMIT 5", (name, like)):
                        rel = clip.replace("\\", "/")
                        if "/clips/" in "/" + rel:
                            rel = ("/" + rel).rsplit("/clips/", 1)[1]
                        candidates.append(rel.lstrip("/"))
            finally:
                db.close()
        except sqlite3.Error:
            pass
        for rel in candidates:
            full = os.path.realpath(os.path.join(base, rel))
            if full.startswith(base + os.sep) and os.path.basename(full) == name and os.path.isfile(full):
                return full
        raise ApiError(404, "not found")


LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <defs>
    <linearGradient id="g" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0" stop-color="#0e7490"/>
      <stop offset="0.6" stop-color="#22d3ee"/>
      <stop offset="1" stop-color="#bbf7d0"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="14" fill="#07111f"/>
  <!-- migration arc with network nodes, growing toward the bird -->
  <path d="M10 53 C 11 43, 17 36, 28 32" fill="none" stroke="#22d3ee" stroke-width="2" stroke-linecap="round" opacity="0.45"/>
  <circle cx="10" cy="53" r="2.8" fill="#0e7490"/>
  <circle cx="14.5" cy="42.5" r="3.3" fill="#1596b3"/>
  <circle cx="23.5" cy="34" r="3.8" fill="#22d3ee"/>
  <!-- swallow in flight, heading up and to the right -->
  <g transform="translate(41 25) rotate(48) scale(1.08)">
    <path d="M0,-10 C1.6,-6.5 2.4,-4.5 15,-7.5 C9,-2 4.5,1.5 2.2,4 L3.6,11.5 L0,8.2 L-3.6,11.5 L-2.2,4 C-4.5,1.5 -9,-2 -15,-7.5 C-2.4,-4.5 -1.6,-6.5 0,-10 Z" fill="url(#g)"/>
  </g>
</svg>
"""  # assets/logo.svg, byte for byte (the Wildlife Network logo is not under the code licence)


DASHBOARD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wildlife Network Base</title>
<link rel="icon" type="image/svg+xml" href="%%FAVICON%%">
<style>
:root{--bg:#0f1412;--card:#18201d;--line:#26302c;--text:#e6ece9;--dim:#93a39c;--accent:#5fd39a;--warn:#f0b357;--bad:#ef6f6c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:10px;margin-bottom:14px}.logo{flex:none;line-height:0}.logo svg{width:36px;height:36px}
h1{font-size:1.3rem;margin:0;flex:1;overflow-wrap:anywhere}h2{font-size:1rem;margin:22px 0 8px;color:var(--dim);font-weight:600}
.dot{width:10px;height:10px;border-radius:50%;background:var(--dim);flex:none}.dot.ok{background:var(--accent)}.dot.bad{background:var(--bad)}
.sub{color:var(--dim);font-size:.85rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.tile b{display:block;font-size:1.15rem}.tile span{color:var(--dim);font-size:.8rem}
ul{list-style:none;margin:0;padding:0}li{display:flex;align-items:center;gap:10px;padding:10px 4px;border-bottom:1px solid var(--line)}
li .t{color:var(--dim);font-variant-numeric:tabular-nums;width:3.2rem;flex:none}li .sp{flex:1;min-width:0}
li .sp i{display:block;overflow-wrap:anywhere}li .sp small{color:var(--dim)}li .c{font-variant-numeric:tabular-nums;color:var(--dim)}
button{font:inherit;color:var(--text);background:#22302a;border:1px solid var(--line);border-radius:8px;padding:8px 12px;cursor:pointer}
button.play{width:44px;height:40px;padding:0;flex:none}button.primary{background:var(--accent);color:#06140d;border:0;font-weight:600}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-top:22px}
summary{cursor:pointer;font-weight:600}label{display:block;margin-top:10px;font-size:.85rem;color:var(--dim)}
input,select{width:100%;font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:4px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.msg{margin-top:10px;font-size:.9rem}.err{color:var(--bad)}.okc{color:var(--accent)}
.empty{color:var(--dim);padding:14px 4px}h3{font-size:.95rem;margin:18px 0 0}details.adv{background:none;border:0;padding:0;margin-top:14px}
.reg{margin:6px 0 0;font-size:.9rem}.reg.ok{color:var(--accent)}.reg.wait{color:var(--warn)}.reg.bad{color:var(--bad);font-weight:600}
label.chk{display:flex;gap:8px;align-items:flex-start;margin-top:14px;color:var(--text)}label.chk input{width:auto;margin:3px 0 0}
</style></head><body><main>
<header><span class="logo" aria-hidden="true">%%LOGO%%</span><h1 id="name">Wildlife Network Base</h1><span id="dot" class="dot" title="Health"></span></header>
<div class="sub" id="hw"></div>
<p class="reg" id="reg"></p>
<p class="reg bad" id="mic" hidden></p>
<p class="reg wait" id="test" hidden>Test station, not on the public map</p>
<h2>Health</h2>
<div class="grid" id="health"></div>
<div class="msg err" id="err"></div>
<h2>Last 24 hours</h2>
<ul id="list"><li class="empty">Loading...</li></ul>
<details id="setup"><summary>Setup</summary>
<p class="sub">Enter the setup code from the label on the Base, a name and the location. Leave a field empty to keep its current value. The Base then connects to Wildlife Network by itself as soon as it has internet.</p>
<form id="form" autocomplete="off">
<label>Setup code<input id="f-code" required autocapitalize="characters" spellcheck="false"></label>
<label>Station name<input id="f-name" maxlength="64"></label>
<div class="row"><label>Latitude<input id="f-lat" inputmode="decimal"></label><label>Longitude<input id="f-lon" inputmode="decimal"></label></div>
<p><button type="button" id="geo">Use this phone's location</button> <span class="sub" id="geomsg"></span></p>
<h3>How the Base gets internet</h3>
<p class="sub">Fill in one of these, or neither if a phone will carry the data out.</p>
<label>4G: APN from your SIM provider<input id="f-apn" spellcheck="false" placeholder="for example internet"></label>
<div class="row"><label>Wi-Fi network<input id="f-ssid" spellcheck="false"></label><label>Wi-Fi password<input id="f-psk" type="password"></label></div>
<label class="chk"><input type="checkbox" id="f-test"> Test station: detections are kept apart and not shown on the public map. Turn it off when the Base is in its real place.</label>
<h3>Alert emails</h3>
<p class="sub">Switching the Base off for a while? Pause the emails about it (no signal, microphone, temperature).</p>
<p class="sub" id="alerts"></p>
<label>Alert emails<select id="f-pause"><option value="">Keep as they are</option><option value="1">Pause for 1 day</option><option value="7">Pause for 1 week</option><option value="30">Pause for 1 month</option><option value="forever">Pause until I turn them back on</option><option value="on">Turn alerts back on</option></select></label>
<details class="adv"><summary>Advanced</summary>
<p class="sub">Only needed if you registered this Base yourself. Otherwise it gets its own key from Wildlife Network.</p>
<label>Wildlife Network API key<input id="f-key" type="password" placeholder="unchanged" spellcheck="false"></label>
<label>Device id<input id="f-dev" spellcheck="false"></label>
<label>Time zone<input id="f-tz" spellcheck="false" placeholder="from the location, for example Asia/Kolkata"></label>
<label class="chk"><input type="checkbox" id="f-auto"> Keep the Base up to date automatically. It checks once a night and installs new versions of its own software by itself. If a new version does not start, the Base goes back to the one before.</label>
<p class="sub" id="upd"></p>
</details>
<p><button class="primary" type="submit">Save</button> <button type="button" id="bngpw">Show BirdNET-Go password</button></p>
<p class="sub" id="bngmsg"></p>
<div class="msg" id="fmsg"></div>
</form></details>
<p class="sub" id="foot"></p>
</main>
<script>
const $=id=>document.getElementById(id);
let audio=null,playing=null,filled=false,tzInit='',autoInit=true;
async function j(url,opt){const r=await fetch(url,opt);const t=await r.json().catch(()=>({}));if(!r.ok)throw new Error(t.error||('HTTP '+r.status));return t}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function ago(iso){if(!iso)return 'never';const s=(Date.now()-new Date(iso).getTime())/1000;if(s<90)return 'just now';if(s<5400)return Math.round(s/60)+' min ago';if(s<172800)return Math.round(s/3600)+' h ago';return Math.round(s/86400)+' days ago'}
function tile(label,value){const d=el('div','tile');d.append(el('b',null,value),el('span',null,label));return d}
async function loadInfo(){const i=await j('/api/info');$('name').textContent=i.name||i.hostname;
const reg=i.registration||'';const rp=$('reg');
rp.className='reg '+(reg==='registered'?'ok':'wait');
rp.textContent=reg==='registered'?'Connected to Wildlife Network':reg==='waiting-for-internet'?'Will connect to Wildlife Network when the Base has internet':reg==='waiting-for-setup'?'Will connect to Wildlife Network once it has a name and location (see Setup below)':reg.startsWith('error: ')?'Not connected to Wildlife Network yet ('+reg.slice(7)+'). It keeps trying.':'';
$('test').hidden=!i.test;const mc=i.microphone||{};$('mic').hidden=mc.found!==false;
$('mic').textContent=mc.found===false?'No microphone found. '+(mc.problem||'')+' Plug a USB microphone into the Base, then restart it.':'';
const al=i.alerts||{};$('alerts').textContent=(al.paused?(al.forever?'Alert emails are paused until you turn them back on.':'Alert emails are paused until '+new Date(al.pausedUntil).toLocaleString()+'.'):'Alert emails are on.')+(al.sent===false?' Wildlife Network gets this change when the Base is online.':'');
const up=i.update||{};$('upd').textContent=up.lastCheck?'Last update check '+ago(up.lastCheck)+': '+(up.lastResult||'')+'.':'No update check yet.';
$('hw').textContent='Hardware '+i.hardwareId+(i.configured?'':' . not set up yet');
const tzw={setting:'set in Setup',location:'from the location',system:'system setting'};
$('foot').textContent=(i.timezone?'Time zone '+i.timezone+' ('+(tzw[i.timezoneSource]||i.timezoneSource)+')'+(i.birdnetGoTimezone&&i.birdnetGoTimezone!==i.timezone?', BirdNET-Go still on '+i.birdnetGoTimezone:'')+' . ':'')+'wnbase '+i.software.wnbase+' . wdx-agent '+(i.software['wdx-agent']||'?')+(i.software.birdnetGo?' . BirdNET-Go '+i.software.birdnetGo:'');
if(!filled){filled=true;$('f-name').value=i.name||'';$('f-lat').value=i.latitude??'';$('f-lon').value=i.longitude??'';$('f-dev').value=i.deviceId||'';$('f-test').checked=!!i.test;tzInit=i.timezoneSetting||'';$('f-tz').value=tzInit;const u=i.update||{};autoInit=u.auto!==false;$('f-auto').checked=autoInit;if(!i.configured)$('setup').open=true}}
async function loadStatus(){const s=await j('/api/status');const h=$('health');h.replaceChildren();
const b=s.battery;h.append(tile('Battery',b&&b.percent!=null?b.percent+'%'+(b.charging?' (charging)':''):'n/a'));
h.append(tile('Storage free',s.storage?(s.storage.freeMb/1000).toFixed(1)+' GB':'n/a'));
h.append(tile('Temperature',s.temperatureC!=null?s.temperatureC+' °C':'n/a'));
h.append(tile('Last upload',ago(s.lastUpload)));
h.append(tile('Waiting to send',s.queue?(s.queue.pending>=500?'500+':String(s.queue.pending)):'n/a'));
const n=s.network||{};let net=n.uplink?n.uplink.toUpperCase():'offline';if(n.cellular&&n.cellular.signalPercent!=null)net+=' '+n.cellular.signalPercent+'%';
h.append(tile('Network',net));$('dot').className='dot '+(s.error?'bad':'ok');$('err').textContent=s.error||''}
function play(btn,clip){if(playing===btn){audio.pause();return}if(audio)audio.pause();
audio=new Audio('/clips/'+encodeURIComponent(clip));playing=btn;btn.textContent='■';
const reset=()=>{btn.textContent='▶';if(playing===btn)playing=null};audio.onended=reset;audio.onpause=reset;audio.onerror=()=>{reset();btn.textContent='!'};audio.play().catch(reset)}
async function loadRecent(){const r=await j('/api/recent?hours=24');const ul=$('list');if(playing)return;ul.replaceChildren();
if(!r.detections.length){ul.append(el('li','empty','No detections in the last 24 hours.'));return}
for(const d of r.detections){const li=el('li');const t=new Date(d.time);
li.append(el('span','t',t.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})));
const sp=el('span','sp');sp.append(el('i',null,d.scientificName));if(d.commonName)sp.append(el('small',null,d.commonName));li.append(sp);
li.append(el('span','c',Math.round(d.confidence*100)+'%'));
if(d.clip){const b=el('button','play','▶');b.type='button';b.setAttribute('aria-label','Play recording');b.onclick=()=>play(b,d.clip);li.append(b)}
ul.append(li)}}
async function refresh(){for(const f of [loadInfo,loadStatus,loadRecent]){try{await f()}catch(e){$('err').textContent=e.message;$('dot').className='dot bad'}}}
$('form').onsubmit=async ev=>{ev.preventDefault();const m=$('fmsg');m.className='msg';m.textContent='Saving...';const b={};
const v=id=>$(id).value.trim();if(v('f-name'))b.name=v('f-name');
if(v('f-lat')||v('f-lon')){b.latitude=Number(v('f-lat'));b.longitude=Number(v('f-lon'))}
if(v('f-key'))b.apiKey=v('f-key');if(v('f-dev'))b.deviceId=v('f-dev');if(v('f-apn'))b.apn=v('f-apn');
if(v('f-ssid'))b.wifi={ssid:v('f-ssid'),psk:$('f-psk').value};b.test=$('f-test').checked;if(v('f-tz')!==tzInit)b.timezone=v('f-tz');if($('f-auto').checked!==autoInit)b.autoUpdate=$('f-auto').checked;
const pz=$('f-pause').value;if(pz==='on')b.alertsPausedUntil=null;else if(pz==='forever')b.alertsPausedUntil='%%FOREVER%%';else if(pz)b.alertsPausedUntil=new Date(Date.now()+Number(pz)*86400000).toISOString();
try{const r=await j('/api/config',{method:'POST',headers:{'content-type':'application/json','x-setup-code':v('f-code')},body:JSON.stringify(b)});
m.className='msg okc';m.textContent='Saved.'+(r.configured?' The Base is set up and connects to Wildlife Network by itself when it has internet.':' The location is still needed.')+(r.timezone?' Time zone '+r.timezone+'.':'')+(r.warnings?' '+r.warnings.join(' '):'');
$('f-key').value='';$('f-psk').value='';$('f-pause').value='';filled=false;refresh()}catch(e){m.className='msg err';m.textContent=e.message}};
$('bngpw').onclick=async()=>{const m=$('bngmsg');try{const r=await j('/api/birdnet-go-password',{method:'POST',headers:{'x-setup-code':$('f-code').value.trim()}});
m.textContent='BirdNET-Go settings (port 8080) password: '+r.password}catch(e){m.textContent=e.message}};
$('geo').onclick=()=>{const g=$('geomsg');
if(!navigator.geolocation){g.textContent='This browser cannot share its location here. Type the coordinates instead (from a map app).';return}
g.textContent='Finding location...';navigator.geolocation.getCurrentPosition(p=>{$('f-lat').value=p.coords.latitude.toFixed(5);$('f-lon').value=p.coords.longitude.toFixed(5);g.textContent='Location filled in.'},
e=>{g.textContent='Could not get the location'+(window.isSecureContext?'':' (many browsers only share it on secure pages)')+'. Type the coordinates instead, for example from a map app.'},{enableHighAccuracy:true,timeout:20000})};
refresh();setInterval(refresh,30000);
</script></body></html>
"""
DASHBOARD = DASHBOARD.replace("%%LOGO%%", LOGO_SVG).replace("%%FOREVER%%", ALERTS_FOREVER).replace(
    "%%FAVICON%%", "data:image/svg+xml," + quote(LOGO_SVG, safe="=:/,.-'()"))


class Handler(BaseHTTPRequestHandler):
    server_version = f"wnbase/{VERSION}"

    def log_message(self, fmt, *args):
        log(f"{self.client_address[0]} {fmt % args}")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_body(self, status: int, body: bytes, ctype: str, cors: bool = False, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, status: int = 200):
        self.send_body(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json; charset=utf-8",
                       cors=self.command == "GET")

    def read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length")
        if n > MAX_BODY:
            raise ApiError(413, "body too large")
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "body must be JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        return body

    @staticmethod
    def int_param(q: dict, key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(q.get(key, [default])[0])))
        except ValueError:
            raise ApiError(400, f"invalid {key}")

    def dispatch(self, method: str):
        app: App = self.server.app  # type: ignore[attr-defined]
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        try:
            if method == "GET":
                if u.path in ("/", "/index.html"):
                    return self.send_body(200, DASHBOARD.encode(), "text/html; charset=utf-8", extra={
                        "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; "
                                                   "style-src 'unsafe-inline'; media-src 'self'; img-src 'self' data:"})
                if u.path == "/api/info":
                    return self.send_json(app.info())
                if u.path == "/api/status":
                    return self.send_json(app.status())
                if u.path == "/api/detections":
                    return self.send_json(app.detections(self.int_param(q, "limit", 500, 1, 500)))
                if u.path == "/api/recent":
                    return self.send_json(app.recent(self.int_param(q, "hours", 24, 1, 168)))
                if u.path.startswith("/clips/"):
                    return self.send_clip(app.clip_path(u.path[len("/clips/"):]))
            elif method == "POST":
                if u.path == "/api/birdnet-go-password":
                    app.check_code(self.client_address[0], self.headers.get("X-Setup-Code") or "")
                    return self.send_json(app.birdnet_password())
                if u.path in ("/api/config", "/api/ack"):
                    body = self.read_json()
                    app.check_code(self.client_address[0], self.headers.get("X-Setup-Code") or "")
                    return self.send_json(app.configure(body) if u.path == "/api/config" else app.ack(body))
            raise ApiError(404, "not found")
        except ApiError as e:
            self.send_json({"error": e.msg}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log(f"error on {method} {u.path}:\n{traceback.format_exc()}")
            try:
                self.send_json({"error": "internal error"}, 500)
            except OSError:
                pass

    def send_clip(self, full: str):
        size = os.path.getsize(full)
        ctype = AUDIO_TYPES[os.path.splitext(full)[1].lower()]
        start, end, status = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                return self.send_body(416, b"", ctype, extra={"Content-Range": f"bytes */{size}"})
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("Access-Control-Allow-Origin", "*")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def make_server(app: App, bind: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind or app.cfg.bind, app.cfg.port if port is None else port), Handler)
    srv.daemon_threads = True
    srv.app = app  # type: ignore[attr-defined]
    return srv


def main() -> None:
    ap = argparse.ArgumentParser(description="Wildlife Network Base local service")
    ap.add_argument("--config", default=os.environ.get("WNBASE_CONFIG", "/etc/wnbase/wnbase.ini"))
    ap.add_argument("--setup-code", action="store_true", help="print the setup code and exit")
    ap.add_argument("--hotspot-env", action="store_true", help="print hotspot settings for hotspot.sh and exit")
    ap.add_argument("--hardware-id", action="store_true", help="print the hardware id and exit")
    args = ap.parse_args()
    cfg = Config(args.config)
    if args.hardware_id:
        print(hardware_id())
        return
    if args.setup_code or args.hotspot_env:
        code, _ = ensure_setup_code(cfg.setup_code_file)
        if args.setup_code:
            print(code)
        else:
            print("\t".join([cfg.hotspot_ifname, hotspot_ssid(cfg, hardware_id()), f"wn-{code}",
                             "1" if cfg.hotspot_force else "0"]))
        return
    app = App(cfg)
    app.apply_timezone_file()
    app.start_timezone()
    app.write_mdns()
    app.start_registration()
    app.start_health()
    srv = make_server(app)
    log(f"wnbase {VERSION} on {cfg.bind}:{cfg.port}, wdx-agent {app.agent.VERSION}, hotspot {hotspot_ssid(cfg, app.hw)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
