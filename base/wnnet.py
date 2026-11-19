#!/usr/bin/env python3
"""wnnet.py: home Wi-Fi first, the Base's hotspot only as a fallback (NetworkManager).

The hotspot profile `wn-hotspot` never connects by itself (autoconnect=no): an access point needs no scan, so at
boot it would take wlan0 before a home Wi-Fi profile has finished scanning, and priorities cannot help because only
one profile can hold wlan0. Instead this service (wn-net.service) decides:

  boot     give home Wi-Fi up to boot_wait seconds (rescan, then try the saved client profiles); hotspot only if
           still not connected, or straight away when there is no client profile at all.
  hotspot  every retry seconds, when no phone is connected to the hotspot, take it down and try home Wi-Fi; if
           that fails, the hotspot comes back. A phone that is setting up the Base is never interrupted.
  client   when home Wi-Fi drops, wait fallback seconds (a router restarting) before starting the hotspot.

Every switch is logged with the reason (journal tag wn-net). Settings in [hotspot] of /etc/wnbase/wnbase.ini:
boot_wait_seconds (90), retry_seconds (300), fallback_seconds (180), force (yes: hotspot always on).

Usage: wnnet.py [--config /etc/wnbase/wnbase.ini]        run the service
       wnnet.py --hotspot                                start the hotspot now (as hotspot.sh --force did)
"""
from __future__ import annotations

import argparse
import configparser
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wnbase  # noqa: E402

HOTSPOT = "wn-hotspot"
UPLINK = "wn-uplink"          # the one home Wi-Fi profile the Base manages
CHECK_EVERY = 15              # seconds between checks while running
CONNECT_WAIT = 45             # seconds nmcli may take to join a network
WIFI_TYPE = "802-11-wireless"
WIFI_FILE = "wildnetwork-wifi.txt"


def log(msg: str) -> None:
    print(msg, flush=True)    # journald tags it wn-net (SyslogIdentifier in wn-net.service)


class Settings:
    def __init__(self, path: str | None = None, **overrides):
        cfg = wnbase.Config(path)
        cp = configparser.ConfigParser(interpolation=None)
        if path:
            cp.read(path)
        h = cp["hotspot"] if cp.has_section("hotspot") else {}

        def num(key, default):
            try:
                return max(0, int(str(h.get(key, "")).strip() or default))
            except ValueError:
                return default
        self.ifname = cfg.hotspot_ifname
        self.force = cfg.hotspot_force
        self.boot_wait = num("boot_wait_seconds", 90)
        self.retry = num("retry_seconds", 300)
        self.fallback = num("fallback_seconds", 180)
        self.ssid, self.psk = "", ""
        self.cfg = cfg
        for k, v in overrides.items():
            setattr(self, k, v)

    def load_hotspot_secret(self) -> None:
        code, _ = wnbase.ensure_setup_code(self.cfg.setup_code_file)
        self.ssid, self.psk = wnbase.hotspot_ssid(self.cfg, wnbase.hardware_id()), f"wn-{code}"


# --- nmcli helpers (also used by wnbase.py for the setup page) ---------------------------------------------------

def client_profiles(run, ifname: str) -> list[str]:
    """Saved Wi-Fi client profiles (not the hotspot) that may use ifname; the managed wn-uplink first."""
    rc, out = run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show"], 15)
    if rc != 0:
        return []
    names = []
    for line in out.splitlines():
        f = wnbase.nmcli_fields(line)
        if len(f) >= 2 and f[1] == WIFI_TYPE and f[0] != HOTSPOT:
            iface = profile_get(run, f[0], "connection.interface-name")
            if iface in ("", ifname):
                names.append(f[0])
    return sorted(names, key=lambda n: (n != UPLINK, n))


def profile_get(run, name: str, field: str) -> str:
    rc, out = run(["nmcli", "-g", field, "connection", "show", name], 15)
    return out.strip() if rc == 0 else ""


def save_uplink(run, ifname: str, ssid: str, psk: str) -> list[str]:
    """Write home Wi-Fi into the one managed profile wn-uplink and switch off every other Wi-Fi client profile on
    ifname (Imager's netplan one, older setups), so only one profile competes for the radio. 4G and Ethernet
    profiles are not touched. Returns warnings."""
    exists = UPLINK in client_profiles(run, ifname) or \
        run(["nmcli", "-g", "connection.id", "connection", "show", UPLINK], 15)[0] == 0
    common = ["802-11-wireless.ssid", ssid, "802-11-wireless.mode", "infrastructure", "wifi-sec.key-mgmt", "wpa-psk",
              "wifi-sec.psk", psk, "connection.interface-name", ifname, "connection.autoconnect", "yes",
              "connection.autoconnect-priority", "10"]
    if exists:
        rc, out = run(["nmcli", "connection", "modify", UPLINK, *common], 30)
    else:
        rc, out = run(["nmcli", "connection", "add", "type", "wifi", "con-name", UPLINK, "ifname", ifname, *common], 30)
    if rc != 0:
        return [f"Wi-Fi network not saved: {out.strip()[:200] or 'nmcli not available'}"]
    warnings = []
    for other in client_profiles(run, ifname):
        if other == UPLINK:
            continue
        rc, out = run(["nmcli", "connection", "modify", other, "connection.autoconnect", "no"], 30)
        if rc == 0:
            log(f"Wi-Fi profile {other!r} switched off (autoconnect no): {UPLINK} is the Base's home Wi-Fi now")
        else:
            warnings.append(f"could not switch off Wi-Fi profile {other}: {out.strip()[:120]}")
    log(f"home Wi-Fi saved in {UPLINK} for {ssid!r}")
    return warnings


def read_wifi_file(text: str) -> dict:
    """wildnetwork-wifi.txt: `ssid=`, `password=`, `country=` lines (also `SSID:` style). Unknown lines ignored."""
    out = {}
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        sep = "=" if "=" in line else ":" if ":" in line else None
        if not sep:
            continue
        k, v = line.split(sep, 1)
        k = k.strip().lower().replace(" ", "")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        key = {"ssid": "ssid", "network": "ssid", "wifi": "ssid", "password": "password", "psk": "password",
               "pass": "password", "country": "country"}.get(k)
        if key:
            out[key] = v
    return out


class Net:
    def __init__(self, s: Settings, run=None, now=None, sleep=None, boot_dir: str | None = None):
        self.s = s
        self.run = run or wnbase.run_command
        self.now = now or time.monotonic
        self.sleep = sleep or time.sleep
        self.boot_dir = boot_dir
        self.lost_since: float | None = None
        self.last_retry: float | None = None
        self.mode = "starting"

    # --- state ---

    def wifi_state(self) -> tuple[str, str]:
        """(state, connection) of the Wi-Fi interface, e.g. ("connected", "wn-uplink")."""
        rc, out = self.run(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"], 15)
        if rc == 0:
            for line in out.splitlines():
                f = wnbase.nmcli_fields(line)
                if len(f) >= 3 and f[0] == self.s.ifname:
                    return f[1], f[2] if f[2] != "--" else ""
        return "", ""

    def stations(self) -> int:
        """Phones connected to the hotspot."""
        rc, out = self.run(["iw", "dev", self.s.ifname, "station", "dump"], 10)
        return sum(1 for x in out.splitlines() if x.startswith("Station ")) if rc == 0 else 0

    def visible(self) -> set[str] | None:
        """SSIDs seen in a fresh scan, or None when the scan failed (e.g. radio busy)."""
        self.run(["nmcli", "device", "wifi", "rescan", "ifname", self.s.ifname], 20)
        rc, out = self.run(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "ifname", self.s.ifname,
                            "--rescan", "no"], 20)
        if rc != 0:
            return None
        return {wnbase.nmcli_fields(x)[0] for x in out.splitlines() if x}

    # --- actions ---

    def ensure_hotspot_profile(self) -> None:
        """Create or update wn-hotspot. autoconnect=no on every start: this also fixes Bases set up before."""
        rc, _ = self.run(["nmcli", "-g", "connection.id", "connection", "show", HOTSPOT], 15)
        if rc == 0:
            self.run(["nmcli", "connection", "modify", HOTSPOT, "802-11-wireless.ssid", self.s.ssid, "wifi-sec.psk",
                      self.s.psk, "connection.interface-name", self.s.ifname, "connection.autoconnect", "no"], 30)
        else:
            self.run(["nmcli", "connection", "add", "type", "wifi", "ifname", self.s.ifname, "con-name", HOTSPOT,
                      "ssid", self.s.ssid, "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                      "ipv4.method", "shared", "ipv4.addresses", "10.42.0.1/24", "ipv6.method", "disabled",
                      "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
                      "wifi-sec.group", "ccmp", "wifi-sec.psk", self.s.psk, "connection.autoconnect", "no"], 30)
            log(f"hotspot profile {HOTSPOT} created (autoconnect no)")

    def hotspot_up(self, why: str) -> bool:
        rc, out = self.run(["nmcli", "connection", "up", HOTSPOT], 60)
        if rc == 0:
            log(f"switch to hotspot {self.s.ssid} (10.42.0.1): {why}")
            self.mode = "hotspot"
            self.last_retry = self.now()
            self.lost_since = None
            return True
        log(f"hotspot did not start ({why}): {out.strip()[:200]}")
        return False

    def hotspot_down(self, why: str) -> None:
        self.run(["nmcli", "connection", "down", HOTSPOT], 30)
        log(f"hotspot down: {why}")

    def try_client(self, why: str) -> str | None:
        """Try the saved home Wi-Fi profiles whose network is in range. Returns the profile that connected."""
        profiles = client_profiles(self.run, self.s.ifname)
        if not profiles:
            return None
        seen = self.visible()
        for name in profiles:
            if profile_get(self.run, name, "connection.autoconnect") == "no" and name != UPLINK:
                continue   # switched off on purpose (save_uplink)
            ssid = profile_get(self.run, name, "802-11-wireless.ssid")
            if seen is not None and ssid and ssid not in seen:
                log(f"home Wi-Fi {ssid!r} ({name}) not in range ({why})")
                continue
            rc, out = self.run(["nmcli", "--wait", str(CONNECT_WAIT), "connection", "up", name], CONNECT_WAIT + 15)
            if rc == 0:
                log(f"switch to home Wi-Fi {ssid!r} ({name}): {why}")
                self.mode, self.lost_since = "client", None
                return name
            log(f"home Wi-Fi {ssid!r} ({name}) did not connect ({why}): {out.strip()[:160]}")
        return None

    def apply_wifi_file(self) -> None:
        """wildnetwork-wifi.txt on the boot partition: save it as home Wi-Fi, then remove the password from the card."""
        boot = self.boot_dir or next((d for d in ("/boot/firmware", "/boot") if os.path.isfile(f"{d}/config.txt")), None)
        if not boot:
            return
        path = os.path.join(boot, WIFI_FILE)
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            return
        w = read_wifi_file(text)
        ssid, psk, country = w.get("ssid", ""), w.get("password", ""), w.get("country", "").upper()
        note = []
        if not ssid or not 8 <= len(psk) <= 63:
            note.append("not applied: needs ssid= and a password= of 8 to 63 characters")
            log(f"{WIFI_FILE}: {note[0]}")
        else:
            if len(country) == 2 and country.isalpha():
                rc, _ = self.run(["raspi-config", "nonint", "do_wifi_country", country], 30)
                if rc != 0:
                    self.run(["iw", "reg", "set", country], 10)
                note.append(f"Wi-Fi country {country}")
            note += save_uplink(self.run, self.s.ifname, ssid, psk)
            log(f"{WIFI_FILE}: home Wi-Fi {ssid!r} applied from the SD card")
        # The password must not stay on the card: keep a note without it, delete the original.
        try:
            Path(path + ".applied").write_text(
                f"Applied {time.strftime('%Y-%m-%d %H:%M:%S')} for ssid={ssid}\n" + "".join(f"{n}\n" for n in note) +
                "The password was removed from the card. To change Wi-Fi again, add a new wildnetwork-wifi.txt.\n")
            with open(path, "r+b") as f:     # overwrite before deleting (FAT keeps no copies, best effort)
                f.write(b"\0" * len(text.encode(errors="replace")))
            os.unlink(path)
        except OSError as e:
            log(f"{WIFI_FILE}: could not remove it from the card: {e}")

    # --- the decisions ---

    def boot(self) -> None:
        self.ensure_hotspot_profile()
        self.apply_wifi_file()
        if self.s.force:
            self.hotspot_up("force = yes in wnbase.ini")
            return
        if not client_profiles(self.run, self.s.ifname):
            self.hotspot_up("no home Wi-Fi saved yet (set it up from a phone)")
            return
        start = self.now()
        tried = False
        while True:
            state, con = self.wifi_state()
            if state == "connected" and con and con != HOTSPOT:
                log(f"home Wi-Fi connected at boot ({con}) after {int(self.now() - start)} s")
                self.mode = "client"
                return
            if self.now() - start >= self.s.boot_wait:
                break
            if not tried and self.now() - start >= min(20, self.s.boot_wait / 2):
                tried = True    # NetworkManager had its chance on its own; scan and ask explicitly
                if self.try_client("boot"):
                    return
                continue
            self.sleep(5)
        self.hotspot_up(f"no home Wi-Fi after {self.s.boot_wait} s at boot")

    def tick(self) -> None:
        state, con = self.wifi_state()
        if con == HOTSPOT and state == "connected":
            self.mode = "hotspot"
            if self.s.force:
                return
            if self.last_retry is None:
                self.last_retry = self.now()
            if self.now() - self.last_retry < self.s.retry:
                return
            self.last_retry = self.now()
            if not client_profiles(self.run, self.s.ifname):
                return
            n = self.stations()
            if n:
                log(f"home Wi-Fi retry skipped: {n} phone(s) on the hotspot")
                return
            self.hotspot_down("trying home Wi-Fi (no phone on the hotspot)")
            if not self.try_client("retry from hotspot"):
                self.hotspot_up("home Wi-Fi still not available")
            return
        if state == "connected" and con:
            if self.mode != "client" or self.lost_since is not None:
                log(f"home Wi-Fi connected ({con})" + (" again" if self.lost_since is not None else ""))
            self.mode, self.lost_since = "client", None
            return
        # Not connected (NetworkManager keeps retrying its saved profiles meanwhile).
        if self.s.force:
            self.hotspot_up("force = yes in wnbase.ini")
            return
        if self.lost_since is None:
            self.lost_since = self.now()
            log(f"Wi-Fi not connected ({state or 'unknown'}); waiting {self.s.fallback} s before the hotspot "
                "(a router may be restarting)")
            return
        if self.now() - self.lost_since >= self.s.fallback:
            if not self.try_client("last try before the hotspot"):
                self.hotspot_up(f"no home Wi-Fi for {self.s.fallback} s")

    def run_forever(self) -> None:
        self.run(["nmcli", "radio", "wifi", "on"], 15)
        self.boot()
        while True:
            self.sleep(CHECK_EVERY)
            try:
                self.tick()
            except Exception as e:   # keep the service alive; systemd would restart it anyway
                log(f"error: {type(e).__name__}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Home Wi-Fi first, hotspot as fallback")
    ap.add_argument("--config", default=os.environ.get("WNBASE_CONFIG", "/etc/wnbase/wnbase.ini"))
    ap.add_argument("--hotspot", action="store_true", help="start the hotspot now and exit")
    a = ap.parse_args()
    s = Settings(a.config)
    s.load_hotspot_secret()
    net = Net(s)
    if a.hotspot:
        net.ensure_hotspot_profile()
        sys.exit(0 if net.hotspot_up("started by hand") else 1)
    log(f"wn-net: {s.ifname}, boot wait {s.boot_wait} s, retry {s.retry} s, fallback {s.fallback} s"
        + (", hotspot forced" if s.force else ""))
    net.run_forever()


if __name__ == "__main__":
    main()
