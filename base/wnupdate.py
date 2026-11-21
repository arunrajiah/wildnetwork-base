#!/usr/bin/env python3
"""wnupdate: keeps a Wildlife Network Base up to date by itself (wnbase-update.service, run by two timers).

  - The Base software (wnbase and the other files in /opt/wnbase, its systemd units) from the latest GitHub release
    of arunrajiah/wildnetwork-base: the release's wnbase-<version>.tar.gz, checked against its .sha256 and against
    the VERSION inside, then installed by that release's own `provision.sh --update`.
  - wdx-agent from <site>/agent/ (VERSION and wdx_agent.py; the VERSION inside the file must match).
  - Never BirdNET-Go (pinned in image/VERSIONS) and never the operating system.

The previous version stays on disk (/opt/wnbase.prev, wdx_agent.py.prev). After installing, the service is restarted
and must come up with the new version (wnbase answers /api/info; wdx-agent logs its version), else the previous
version is put back and restarted. Settings, keys, the setup code, the time zone and Wi-Fi are never touched.

Usage:
  wnupdate.py --config /etc/wnbase/wnbase.ini          check and update (skipped when switched off in Setup)
  wnupdate.py --config ... --now                      also when switched off (run by hand)
  wnupdate.py --config ... --check                    only look up the newest versions
Log: journalctl -t wnbase-update. Last result: /var/lib/wnbase/update.json. Standard library only.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = "arunrajiah/wildnetwork-base"
RELEASES_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = "WildlifeNetworkBase-updater"
MAX_DOWNLOAD = 30 * 1024 * 1024
STATE_FILE = "update.json"
OFF_VALUES = ("0", "no", "false", "off")
# Units the Base software installs; kept with the previous version so a rollback restores them too.
UNITS = ("wnbase.service", "wn-net.service", "wdx-agent.service", "wnbase-update.service", "wnbase-update.timer",
         "wnbase-update-boot.timer")
UPLINK_TYPES = ("gsm", "802-3-ethernet", "802-11-wireless")
START_WAIT = 90          # seconds a restarted service gets to come up with the new version


def log(msg: str) -> None:
    print(msg, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- pure decisions (tested) -------------------------------------------------------------------------------------

def parse_version(v) -> tuple[int, ...] | None:
    """'v0.3.0' or '0.3.0' -> (0, 3, 0); anything else (pre-releases included) -> None."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+){0,3})", str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(latest, current) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None:
        return False
    if b is None:
        return True    # current unknown (for example no wdx-agent yet)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def switch_on(raw) -> bool:
    """[base] auto_update in wnbase.ini: on unless set to no/false/off/0 (missing or empty: on)."""
    return str(raw if raw is not None else "").strip().lower() not in OFF_VALUES


def should_run(raw, force: bool = False) -> bool:
    """A run by hand (--now) goes ahead even when the owner switched updates off; the timer only when on."""
    return True if force else switch_on(raw)


def has_uplink(run) -> bool:
    """An active Wi-Fi, Ethernet or 4G connection other than the Base's own hotspot (same rule as wnbase's uplink)."""
    rc, out = run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"], 15)
    if rc != 0:
        return False
    for line in out.splitlines():
        f = nmcli_fields(line)
        if len(f) >= 2 and f[1] in UPLINK_TYPES and f[0] != "wn-hotspot":
            return True
    return False


def came_up(expected: str, reported) -> bool:
    """Keep the new version only when the restarted service reports exactly it; anything else rolls back."""
    return reported is not None and str(reported) == str(expected)


def file_version(text: str) -> str | None:
    m = re.search(r'^VERSION = "([^"]+)"$', text, re.M)
    return m.group(1) if m else None


def sha256_from(text: str) -> str | None:
    """First token of a sha256sum line ('<hex>  file')."""
    tok = (text.split() or [""])[0].lower()
    return tok if re.fullmatch(r"[0-9a-f]{64}", tok) else None


def release_assets(rel: dict) -> tuple[str | None, str | None, str | None]:
    """(version, tarball url, sha256 url) from a GitHub release object; urls None when the assets are missing."""
    ver = str(rel.get("tag_name") or "").lstrip("v")
    if rel.get("draft") or rel.get("prerelease") or parse_version(ver) is None:
        return None, None, None
    urls = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets") or []}
    return ver, urls.get(f"wnbase-{ver}.tar.gz"), urls.get(f"wnbase-{ver}.tar.gz.sha256")


def nmcli_fields(line: str) -> list[str]:
    """Split one line of `nmcli -t` output (':' separates fields; '\\:' is a literal colon)."""
    return [p.replace("\x00", ":") for p in line.replace("\\:", "\x00").split(":")]


# --- I/O ---------------------------------------------------------------------------------------------------------

def run_command(args: list[str], timeout: int = 60, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT, "accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError(f"{url} is larger than {MAX_DOWNLOAD} bytes")
    return data


def read_state(state_dir: str) -> dict:
    try:
        st = json.loads(Path(state_dir, STATE_FILE).read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state_dir: str, st: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = Path(state_dir, STATE_FILE)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class Updater:
    def __init__(self, config: str | None = "/etc/wnbase/wnbase.ini", run=None, fetch=None, sleep=None, **paths):
        self.config = config
        self.run = run or run_command
        self.fetch = fetch or http_get
        self.sleep = sleep or time.sleep
        cp = configparser.ConfigParser(interpolation=None)
        if config:
            cp.read(config)
        w = cp["wnbase"] if cp.has_section("wnbase") else {}
        self.switch = cp.get("base", "auto_update", fallback="") if cp.has_section("base") else ""
        self.port = int((w.get("port") or "80").strip() or 80)
        self.agent_config = (w.get("agent_config") or "").strip() or "/etc/wdx-agent.ini"
        self.agent_path = (w.get("agent_module") or "").strip() or "/opt/wdx-agent/wdx_agent.py"
        self.agent_service = (w.get("agent_service") or "").strip() or "wdx-agent"
        self.state_dir = (w.get("state_dir") or "").strip() or "/var/lib/wnbase"
        self.opt_dir = "/opt/wnbase"
        self.prev_dir = "/opt/wnbase.prev"
        self.unit_dir = "/etc/systemd/system"
        self.releases_url = RELEASES_LATEST
        for k, v in paths.items():
            setattr(self, k, v)

    # --- versions ------------------------------------------------------------------------------------------------

    def site(self) -> str:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(self.agent_config)
        ep = cp.get("agent", "endpoint", fallback="") if cp.has_section("agent") else ""
        return (ep.strip() or "https://wlnw.org/api/v1/events").split("/api/", 1)[0]

    def current_base(self) -> str | None:
        try:
            return file_version(Path(self.opt_dir, "wnbase.py").read_text())
        except OSError:
            return None

    def current_agent(self) -> str | None:
        try:
            return file_version(Path(self.agent_path).read_text())
        except OSError:
            return None

    def latest_base(self) -> tuple[str | None, str | None, str | None]:
        return release_assets(json.loads(self.fetch(self.releases_url, 30)))

    def latest_agent(self) -> str | None:
        v = self.fetch(f"{self.site()}/agent/VERSION", 30).decode("utf-8", "replace").strip()
        return v if parse_version(v) else None

    # --- the run -------------------------------------------------------------------------------------------------

    def check(self, force: bool = False, install: bool = True) -> dict:
        st = read_state(self.state_dir)
        st["lastCheck"] = now_iso()
        if not should_run(self.switch, force):
            return self.finish(st, "skipped: automatic updates are switched off")
        if not has_uplink(self.run):
            return self.finish(st, "skipped: no internet connection (hotspot only or offline)")
        latest = dict(st.get("latest") or {})
        errors = []
        base_ver = tar_url = sha_url = None
        try:
            base_ver, tar_url, sha_url = self.latest_base()
            latest["wnbase"] = base_ver
        except (OSError, ValueError) as e:
            errors.append(f"newest Base version not found ({type(e).__name__}: {str(e)[:80]})")
        agent_ver = None
        try:
            agent_ver = self.latest_agent()
            latest["wdx-agent"] = agent_ver
        except (OSError, ValueError) as e:
            errors.append(f"newest wdx-agent version not found ({type(e).__name__}: {str(e)[:80]})")
        st["latest"] = latest
        cur_base, cur_agent = self.current_base(), self.current_agent()
        log(f"installed: wnbase {cur_base}, wdx-agent {cur_agent}; newest: wnbase {base_ver}, wdx-agent {agent_ver}")
        done: list[str] = []
        if install and base_ver and is_newer(base_ver, cur_base):
            if tar_url and sha_url:
                done.append(self.update_base(base_ver, tar_url, sha_url, cur_base))
            else:
                errors.append(f"release {base_ver} has no update files (wnbase-{base_ver}.tar.gz and .sha256)")
        if install and agent_ver and is_newer(agent_ver, cur_agent):
            done.append(self.update_agent(agent_ver, cur_agent))
        if not install:
            result = "checked"
        elif done:
            result = "; ".join(done)
        elif errors:
            result = "error: " + "; ".join(errors)
        else:
            result = "up to date"
        if done and errors:
            result += "; " + "; ".join(errors)
        return self.finish(st, result)

    def finish(self, st: dict, result: str) -> dict:
        st["lastResult"] = result
        log(result)
        try:
            write_state(self.state_dir, st)
        except OSError as e:
            log(f"could not write {self.state_dir}/{STATE_FILE}: {e}")
        return st

    # --- Base software -------------------------------------------------------------------------------------------

    def run_user(self) -> str:
        try:
            m = re.search(r"^User=(\S+)", Path(self.unit_dir, "wdx-agent.service").read_text(), re.M)
            if m:
                return m.group(1)
        except OSError:
            pass
        rc, out = self.run(["id", "-nu", "1000"], 10)
        return out.strip() if rc == 0 and out.strip() else "pi"

    def update_base(self, ver: str, tar_url: str, sha_url: str, cur: str | None) -> str:
        log(f"updating the Base software {cur} -> {ver}")
        with tempfile.TemporaryDirectory(prefix="wnupdate-") as tmp:
            try:
                data = self.fetch(tar_url, 120)
                want = sha256_from(self.fetch(sha_url, 30).decode("utf-8", "replace"))
            except (OSError, ValueError) as e:
                return f"error: Base {ver} not downloaded ({type(e).__name__})"
            if not want or hashlib.sha256(data).hexdigest() != want:
                return f"error: Base {ver} download does not match its sha256; nothing changed"
            src = self.unpack(data, tmp)
            if src is None:
                return f"error: Base {ver} update file has no base/wnbase.py; nothing changed"
            inside = file_version(Path(src, "base", "wnbase.py").read_text())
            if inside != ver:
                return f"error: Base update file holds version {inside}, not {ver}; nothing changed"
            for py in Path(src, "base").glob("*.py"):
                try:
                    compile(py.read_text(), str(py), "exec")
                except (SyntaxError, ValueError) as e:
                    return f"error: Base {ver} {py.name} does not compile ({e}); nothing changed"
            self.backup_base()
            env = dict(os.environ, WN_USER=self.run_user(), WN_SRC=src)
            rc, out = self.run(["bash", os.path.join(src, "base", "provision.sh"), "--update"], 300, env)
            if rc != 0:
                log(f"provision.sh --update failed ({rc}): {out.strip()[-400:]}")
                return self.rollback_base(ver, cur, "install failed")
            reported = self.restart_wnbase()
            if not came_up(ver, (reported or {}).get("wnbase")):
                return self.rollback_base(ver, cur, f"wnbase did not come up as {ver} (reported {reported})")
            if self.changed("wnnet.py"):
                # wn-net leaves a working home Wi-Fi connection as it is; it only re-reads its own code.
                self.run(["systemctl", "restart", "wn-net.service"], 60)
        log(f"Base software {ver} installed and running (previous version kept in {self.prev_dir})")
        return f"updated wnbase {cur} -> {ver}"

    @staticmethod
    def unpack(data: bytes, dest: str) -> str | None:
        """Extract the tarball safely (no absolute paths, links out of the folder or devices); returns the folder
        that holds base/wnbase.py."""
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(dest, filter="data")
        for root, _dirs, files in os.walk(dest):
            if "wnbase.py" in files and os.path.basename(root) == "base":
                return os.path.dirname(root)
        return None

    def backup_base(self) -> None:
        shutil.rmtree(self.prev_dir, ignore_errors=True)
        shutil.copytree(self.opt_dir, self.prev_dir, symlinks=True)
        units = Path(self.prev_dir, ".systemd")
        units.mkdir(exist_ok=True)
        for u in UNITS:
            if Path(self.unit_dir, u).is_file():
                shutil.copy2(Path(self.unit_dir, u), units / u)

    def changed(self, name: str) -> bool:
        try:
            return Path(self.opt_dir, name).read_bytes() != Path(self.prev_dir, name).read_bytes()
        except OSError:
            return True

    def rollback_base(self, ver: str, cur: str | None, why: str) -> str:
        log(f"rolling back to {cur}: {why}")
        units = Path(self.prev_dir, ".systemd")
        for u in UNITS:
            if (units / u).is_file():
                shutil.copy2(units / u, Path(self.unit_dir, u))
            elif u.startswith("wnbase-update") and Path(self.unit_dir, u).exists():
                # added by the failed version: the previous one did not have it
                self.run(["systemctl", "disable", u], 30)
                Path(self.unit_dir, u).unlink(missing_ok=True)
        staging = self.opt_dir + ".failed"
        shutil.rmtree(staging, ignore_errors=True)
        os.replace(self.opt_dir, staging)
        shutil.copytree(self.prev_dir, self.opt_dir, symlinks=True, ignore=shutil.ignore_patterns(".systemd"))
        shutil.rmtree(staging, ignore_errors=True)
        self.run(["systemctl", "daemon-reload"], 60)
        reported = self.restart_wnbase()
        back = (reported or {}).get("wnbase")
        if came_up(cur, back):
            return f"rolled back: wnbase {ver} failed ({why}); {cur} running again"
        return f"rolled back: wnbase {ver} failed ({why}); {cur} put back but not answering yet (reported {back})"

    def info(self) -> dict | None:
        try:
            return json.loads(self.fetch(f"http://127.0.0.1:{self.port}/api/info", 10))
        except (OSError, ValueError):
            return None

    def restart_wnbase(self) -> dict | None:
        """Restart wnbase and wait for /api/info; returns its `software` versions, or None."""
        self.run(["systemctl", "reset-failed", "wnbase.service"], 30)
        self.run(["systemctl", "restart", "wnbase.service"], 60)
        waited, last = 0, None
        while waited < START_WAIT:
            self.sleep(3)
            waited += 3
            i = self.info()
            if i and isinstance(i.get("software"), dict):
                last = i["software"]
                return last
        return last

    # --- wdx-agent -----------------------------------------------------------------------------------------------

    def agent_running_version(self) -> str | None:
        """The version this run of the service logged ("wdx-agent X source=..."), waiting up to START_WAIT."""
        waited = 0
        while waited < START_WAIT:
            self.sleep(3)
            waited += 3
            rc, inv = self.run(["systemctl", "show", "-p", "InvocationID", "--value", self.agent_service], 10)
            if rc != 0 or not inv.strip():
                continue
            rc, out = self.run(["journalctl", f"_SYSTEMD_INVOCATION_ID={inv.strip()}", "-o", "cat", "--no-pager"], 15)
            found = re.findall(r"wdx-agent ([0-9][0-9.]*) source=", out)
            if found:
                return found[-1]
        return None

    def agent_active(self) -> bool:
        return self.run(["systemctl", "is-active", "--quiet", self.agent_service], 10)[0] == 0

    def update_agent(self, ver: str, cur: str | None) -> str:
        log(f"updating wdx-agent {cur} -> {ver}")
        try:
            data = self.fetch(f"{self.site()}/agent/wdx_agent.py", 60)
        except (OSError, ValueError) as e:
            return f"error: wdx-agent {ver} not downloaded ({type(e).__name__})"
        text = data.decode("utf-8", "replace")
        inside = file_version(text)
        if inside != ver:
            return f"error: downloaded wdx-agent says {inside}, not {ver}; nothing changed"
        if "def pending_batch" not in text:
            return "error: downloaded wdx_agent.py is not wdx-agent; nothing changed"
        try:
            compile(text, "wdx_agent.py", "exec")
        except (SyntaxError, ValueError) as e:
            return f"error: wdx-agent {ver} does not compile ({e}); nothing changed"
        was_active = self.agent_active()
        prev = self.agent_path + ".prev"
        if os.path.exists(self.agent_path):
            shutil.copy2(self.agent_path, prev)
        self.install_file(self.agent_path, data)
        ok, why = self.restart_agent(ver, was_active)
        if ok:
            log(f"wdx-agent {ver} installed and running (previous version kept as {prev})")
            return f"updated wdx-agent {cur} -> {ver}"
        log(f"rolling back wdx-agent to {cur}: {why}")
        if os.path.exists(prev):
            self.install_file(self.agent_path, Path(prev).read_bytes())
        self.restart_agent(cur or "", was_active)
        return f"rolled back: wdx-agent {ver} failed ({why}); {cur} running again"

    def restart_agent(self, ver: str, was_active: bool) -> tuple[bool, str]:
        """Restart wdx-agent (when it was running) and wnbase, which loads the agent's code; both must report ver."""
        self.run(["systemctl", "reset-failed", self.agent_service], 30)
        if was_active:
            self.run(["systemctl", "restart", self.agent_service], 60)
            running = self.agent_running_version()
            if not came_up(ver, running):
                return False, f"wdx-agent reported {running}"
        # Not running (no key yet: the Base works in pickup mode): wnbase loading it is the check.
        sw = self.restart_wnbase() or {}
        if not came_up(ver, sw.get("wdx-agent")):
            return False, f"wnbase reported wdx-agent {sw.get('wdx-agent')}"
        return True, ""

    @staticmethod
    def install_file(path: str, data: bytes) -> None:
        tmp = f"{path}.wnupdate-tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Keep the Wildlife Network Base software and wdx-agent up to date")
    ap.add_argument("--config", default=os.environ.get("WNBASE_CONFIG", "/etc/wnbase/wnbase.ini"))
    ap.add_argument("--now", action="store_true", help="run even when automatic updates are switched off")
    ap.add_argument("--check", action="store_true", help="only look up the newest versions, install nothing")
    args = ap.parse_args()
    st = Updater(args.config).check(force=args.now or args.check, install=not args.check)
    sys.exit(1 if str(st.get("lastResult", "")).startswith(("error", "rolled back")) else 0)


if __name__ == "__main__":
    main()
