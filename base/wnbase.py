#!/usr/bin/env python3
"""wnbase: the local web service of a WildNetwork Base.

Serves a dashboard and a small JSON API over the Base's own Wi-Fi hotspot (http://10.42.0.1) for the
WildNetwork field app and for any browser. Detections, cursors and health come from wdx-agent, which is
imported from its own file so there is one copy of that logic. Standard library only.

Usage:
  wnbase.py --config /etc/wnbase/wnbase.ini          run the service
  wnbase.py --config ... --setup-code                print the setup code (created on first use)
  wnbase.py --config ... --hotspot-env               print "ifname<TAB>ssid<TAB>password<TAB>force" for hotspot.sh
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import hmac
import importlib.util
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
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

VERSION = "0.1.0"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I, L, O, 0 or 1
CODE_LENGTH = 8
FAIL_LIMIT = 10        # wrong setup codes allowed per client address ...
FAIL_WINDOW = 600      # ... in this many seconds
MAX_BODY = 64 * 1024
RECENT_MAX = 200
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
        self.setup_code_file = g(w, "setup_code_file", "/etc/wnbase/setup-code")
        self.hotspot_ifname = g(h, "ifname", "wlan0")
        self.hotspot_ssid_prefix = g(h, "ssid_prefix", "WildNetwork-")
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


def write_ini(path: str, section: str, updates: dict[str, str]) -> None:
    """Change some keys of an ini file atomically, keeping every other key, the file mode and the owner."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(path)
    if not cp.has_section(section):
        cp.add_section(section)
    for k, v in updates.items():
        cp[section][k] = v
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None
    tmp = f"{path}.wnbase-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        cp.write(f)
        f.flush()
        os.fsync(f.fileno())
    if st is not None:
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.chmod(tmp, st.st_mode & 0o777)
    else:
        os.chmod(tmp, 0o640)
    os.replace(tmp, path)


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


class App:
    def __init__(self, cfg: Config, runner=None, agent=None):
        self.cfg = cfg
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

    # --- helpers -----------------------------------------------------------------------------------------

    def settings(self):
        try:
            return self.agent.Settings(self.cfg.agent_config)
        except SystemExit as e:
            raise ApiError(503, f"wdx-agent config: {e}")

    def db_path(self, s) -> str:
        return self.cfg.birdnet_db or s.path

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
        return bool(s and s.latitude is not None and s.longitude is not None and (s.api_key or s.device_id))

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
            "name": (s.station_name or None) if s else None,
            "latitude": s.latitude if s else None,
            "longitude": s.longitude if s else None,
        }

    def configure(self, body: dict) -> dict:
        allowed = {"name", "latitude", "longitude", "roundCoords", "apiKey", "deviceId", "endpoint", "apn", "wifi"}
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
        if "roundCoords" in body:
            v = body["roundCoords"]
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 5:
                raise ApiError(400, "invalid roundCoords")
            updates["round_coords"] = str(v)
        if "apiKey" in body:
            updates["api_key"] = text("apiKey", r"[\x21-\x7e]*", 200)
        if "deviceId" in body:
            updates["device_id"] = text("deviceId", r"[A-Za-z0-9_.:-]*", 100)
        if "endpoint" in body:
            updates["endpoint"] = text("endpoint", r"https?://[\x21-\x7e]+", 300)
        apn = text("apn", r"[A-Za-z0-9._-]{1,63}", 63) if "apn" in body else None
        wifi = body.get("wifi")
        if wifi is not None:
            if not isinstance(wifi, dict) or not isinstance(wifi.get("ssid"), str) or not isinstance(wifi.get("psk"), str):
                raise ApiError(400, "invalid wifi")
            if not 1 <= len(wifi["ssid"].encode()) <= 32 or not re.fullmatch(r"[\x20-\x7e]{8,63}", wifi["psk"]):
                raise ApiError(400, "invalid wifi: ssid 1 to 32 bytes, psk 8 to 63 characters")

        warnings: list[str] = []
        if updates:
            with self.lock:
                write_ini(self.cfg.agent_config, "agent", updates)
            log(f"config updated: {', '.join(sorted(updates))}")
        if apn is not None:
            warnings += self.set_apn(apn)
        if wifi is not None:
            warnings += self.set_wifi(wifi["ssid"], wifi["psk"])
        if updates:
            rc, out = self.run(["systemctl", "restart", self.cfg.agent_service], 60)
            if rc != 0:
                warnings.append(f"could not restart {self.cfg.agent_service}: {out.strip()[:200]}")
        try:
            configured = self.configured(self.settings())
        except ApiError:
            configured = False
        res = {"ok": True, "configured": configured}
        if warnings:
            res["warnings"] = warnings
        return res

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
        if self.connection_exists("wn-uplink"):
            self.run(["nmcli", "connection", "delete", "wn-uplink"], 30)
        rc, out = self.run(["nmcli", "connection", "add", "type", "wifi", "ifname", self.cfg.hotspot_ifname,
                            "con-name", "wn-uplink", "ssid", ssid, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk,
                            "connection.autoconnect", "yes", "connection.autoconnect-priority", "10"], 30)
        if rc != 0:
            return [f"Wi-Fi uplink not saved: {out.strip()[:200] or 'nmcli not available'}"]
        # Not brought up now: the radio is serving this hotspot. It joins the network at the next boot, or when
        # the hotspot is switched off, and the hotspot comes back whenever no uplink is connected.
        log(f"Wi-Fi uplink wn-uplink saved for {ssid!r}")
        return []

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
                    for rid, date, clock, sci, com, conf, clip in db.execute(q, (d, d, t, RECENT_MAX)):
                        rows.append({"id": rid, "time": self.agent.local_iso(date, clock), "scientificName": sci,
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


DASHBOARD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WildNetwork Base</title>
<style>
:root{--bg:#0f1412;--card:#18201d;--line:#26302c;--text:#e6ece9;--dim:#93a39c;--accent:#5fd39a;--warn:#f0b357;--bad:#ef6f6c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:10px;margin-bottom:14px}
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
input{width:100%;font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:4px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.msg{margin-top:10px;font-size:.9rem}.err{color:var(--bad)}.okc{color:var(--accent)}
.empty{color:var(--dim);padding:14px 4px}
</style></head><body><main>
<header><span id="dot" class="dot"></span><h1 id="name">WildNetwork Base</h1></header>
<div class="sub" id="hw"></div>
<h2>Health</h2>
<div class="grid" id="health"></div>
<div class="msg err" id="err"></div>
<h2>Last 24 hours</h2>
<ul id="list"><li class="empty">Loading...</li></ul>
<details id="setup"><summary>Setup</summary>
<p class="sub">Enter the setup code from the label on the Base. Leave a field empty to keep its current value.</p>
<form id="form" autocomplete="off">
<label>Setup code<input id="f-code" required autocapitalize="characters" spellcheck="false"></label>
<label>Station name<input id="f-name" maxlength="64"></label>
<div class="row"><label>Latitude<input id="f-lat" inputmode="decimal"></label><label>Longitude<input id="f-lon" inputmode="decimal"></label></div>
<label>WildNetwork API key<input id="f-key" type="password" placeholder="unchanged" spellcheck="false"></label>
<label>Device id<input id="f-dev" spellcheck="false"></label>
<label>4G APN (from your SIM provider)<input id="f-apn" spellcheck="false"></label>
<div class="row"><label>Wi-Fi network<input id="f-ssid" spellcheck="false"></label><label>Wi-Fi password<input id="f-psk" type="password"></label></div>
<p><button class="primary" type="submit">Save</button></p>
<div class="msg" id="fmsg"></div>
</form></details>
<p class="sub" id="foot"></p>
</main>
<script>
const $=id=>document.getElementById(id);
let audio=null,playing=null,filled=false;
async function j(url,opt){const r=await fetch(url,opt);const t=await r.json().catch(()=>({}));if(!r.ok)throw new Error(t.error||('HTTP '+r.status));return t}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function ago(iso){if(!iso)return 'never';const s=(Date.now()-new Date(iso).getTime())/1000;if(s<90)return 'just now';if(s<5400)return Math.round(s/60)+' min ago';if(s<172800)return Math.round(s/3600)+' h ago';return Math.round(s/86400)+' days ago'}
function tile(label,value){const d=el('div','tile');d.append(el('b',null,value),el('span',null,label));return d}
async function loadInfo(){const i=await j('/api/info');$('name').textContent=i.name||i.hostname;
$('hw').textContent='Hardware '+i.hardwareId+(i.configured?'':' . not set up yet');
$('foot').textContent='wnbase '+i.software.wnbase+' . wdx-agent '+(i.software['wdx-agent']||'?')+(i.software.birdnetGo?' . BirdNET-Go '+i.software.birdnetGo:'');
if(!filled){filled=true;$('f-name').value=i.name||'';$('f-lat').value=i.latitude??'';$('f-lon').value=i.longitude??'';$('f-dev').value=i.deviceId||'';if(!i.configured)$('setup').open=true}}
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
if(v('f-ssid'))b.wifi={ssid:v('f-ssid'),psk:$('f-psk').value};
try{const r=await j('/api/config',{method:'POST',headers:{'content-type':'application/json','x-setup-code':v('f-code')},body:JSON.stringify(b)});
m.className='msg okc';m.textContent='Saved.'+(r.configured?' The Base is set up.':' Location and a key or device id are still needed.')+(r.warnings?' '+r.warnings.join(' '):'');
$('f-key').value='';$('f-psk').value='';filled=false;refresh()}catch(e){m.className='msg err';m.textContent=e.message}};
refresh();setInterval(refresh,30000);
</script></body></html>
"""


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
    ap = argparse.ArgumentParser(description="WildNetwork Base local service")
    ap.add_argument("--config", default=os.environ.get("WNBASE_CONFIG", "/etc/wnbase/wnbase.ini"))
    ap.add_argument("--setup-code", action="store_true", help="print the setup code and exit")
    ap.add_argument("--hotspot-env", action="store_true", help="print hotspot settings for hotspot.sh and exit")
    args = ap.parse_args()
    cfg = Config(args.config)
    if args.setup_code or args.hotspot_env:
        code, _ = ensure_setup_code(cfg.setup_code_file)
        if args.setup_code:
            print(code)
        else:
            print("\t".join([cfg.hotspot_ifname, hotspot_ssid(cfg, hardware_id()), f"wn-{code}",
                             "1" if cfg.hotspot_force else "0"]))
        return
    app = App(cfg)
    srv = make_server(app)
    log(f"wnbase {VERSION} on {cfg.bind}:{cfg.port}, wdx-agent {app.agent.VERSION}, hotspot {hotspot_ssid(cfg, app.hw)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
