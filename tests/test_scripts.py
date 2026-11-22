"""Shell scripts in base/ and install.sh, run with fakes where they touch the system."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SetupFile(unittest.TestCase):
    def test_writes_the_details(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(["bash", str(ROOT / "base" / "setup-file.sh"), "wildnetwork-base-ab12",
                                "Wildlife Network-AB12", "ABCD2345", d], capture_output=True, text=True, check=True)
            text = Path(d, "wildnetwork-setup.txt").read_text()
            self.assertIn("Wi-Fi:       Wildlife Network-AB12\n", text)
            self.assertIn("Password:    wn-ABCD2345\n", text)
            self.assertIn("Setup code:  ABCD2345\n", text)
            self.assertIn("wildnetwork-setup.txt", r.stdout)

    def test_no_boot_partition_is_not_an_error(self):
        r = subprocess.run(["bash", str(ROOT / "base" / "setup-file.sh"), "n", "s", "c", "/nonexistent-boot"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("not written", r.stdout)


class Bootstrap(unittest.TestCase):
    def test_firstboot_env_only_known_keys(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d, "wildnetwork-firstboot.env")
            marker = Path(d, "ran")
            env.write_text(f"# settings\nWN_WIFI_COUNTRY=IN\r\nWN_REF=\"v0.2.0\"\nEVIL=$(touch {marker})\n"
                           f"WN_USER='pi'\nPATH=/nowhere\n")
            clean = {k: v for k, v in os.environ.items() if not k.startswith("WN_")}
            r = subprocess.run(["bash", str(ROOT / "bootstrap.sh"), "--print-env", str(env)],
                               capture_output=True, text=True, check=True, env=clean)
            self.assertEqual(sorted(x for x in r.stdout.splitlines() if x.startswith("WN_")),
                             ["WN_REF=v0.2.0", "WN_USER=pi", "WN_WIFI_COUNTRY=IN"])
            self.assertIn("ignoring unknown setting EVIL", r.stdout)
            self.assertFalse(marker.exists())

    def test_environment_wins_over_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d, "e.env")
            env.write_text("WN_WIFI_COUNTRY=IN\n")
            r = subprocess.run(["bash", str(ROOT / "bootstrap.sh"), "--print-env", str(env)],
                               capture_output=True, text=True, check=True, env=dict(os.environ, WN_WIFI_COUNTRY="GB"))
            self.assertIn("WN_WIFI_COUNTRY=GB", r.stdout)


class InstallScript(unittest.TestCase):
    def test_syntax(self):
        for f in ["install.sh", "bootstrap.sh", *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "base").glob("*.sh"))]:
            subprocess.run(["bash", "-n", str(ROOT / f)], check=True)

    def test_pinned_birdnet_go_version(self):
        line = next(x for x in (ROOT / "image" / "VERSIONS").read_text().splitlines() if x.startswith("BIRDNET_GO_IMAGE="))
        r = subprocess.run(["sed", "-n", r"s/^BIRDNET_GO_IMAGE=.*:\([^:]*\)$/\1/p", str(ROOT / "image" / "VERSIONS")],
                           capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.strip(), line.rsplit(":", 1)[1])
        self.assertIn('BNG_VERSION="${WN_BIRDNET_VERSION:-${BNG_PINNED:-latest}}"', (ROOT / "install.sh").read_text())

    def test_install_log(self):
        text = (ROOT / "install.sh").read_text()
        self.assertIn('exec > >(tee -a "$LOG") 2>&1', text)
        self.assertIn("/var/log/wnbase-install.log", text)


class Image(unittest.TestCase):
    def test_build_includes_every_installed_file(self):
        prov = (ROOT / "base" / "provision.sh").read_text()
        line = next(x for x in prov.splitlines() if x.startswith("for f in ") and x.endswith("; do"))
        for f in line[len("for f in "):-len("; do")].split():
            self.assertTrue((ROOT / "base" / f).is_file(), f)
        self.assertIn("mic.py", line)
        build = (ROOT / "image" / "build.sh").read_text()
        self.assertIn('cp -R "$ROOT/base" "$ROOT/systemd" "$ROOT/apt" "$ROOT/install.sh"', build)
        # Every apt/ file provision.sh installs exists (the whole folder goes into the image and the update package).
        import re
        for f in re.findall(r'"\$SRC/apt/([^"]+)"', prov):
            self.assertTrue((ROOT / "apt" / f).is_file(), f)
        self.assertIn("sha256sum", build)

    def test_security_updates_only(self):
        conf = (ROOT / "apt" / "52wnbase-unattended-upgrades").read_text()
        self.assertIn("#clear Unattended-Upgrade::Origins-Pattern;", conf)
        patterns = [l.strip() for l in conf.splitlines() if l.strip().startswith('"origin=')]
        self.assertTrue(patterns)
        self.assertTrue(all("label=Debian-Security" in p for p in patterns), patterns)
        prov = (ROOT / "base" / "provision.sh").read_text()
        # Installed in every mode (not inside the "not --update" block), and never fatal during an update.
        section = prov.split("# --- operating system security updates", 1)[1].split("# --- services", 1)[0]
        self.assertIn('if [ "$MODE" = --update ]', section)  # handles self-updating Bases, so it runs in every mode
        self.assertIn("|| warn", prov)

    def test_release_attaches_checksum(self):
        wf = (ROOT / ".github" / "workflows" / "image.yml").read_text()
        self.assertIn('tags: ["v*"]', wf)
        self.assertIn("image/deploy/*.img.xz.sha256", wf.split("Attach to the release", 1)[1])


class AgentUnit(unittest.TestCase):
    def test_after_network_online(self):
        unit = (ROOT / "systemd" / "wdx-agent.service").read_text()
        self.assertIn("After=network-online.target", unit)
        self.assertIn("Wants=network-online.target", unit)
        self.assertIn("NetworkManager-wait-online.service", (ROOT / "base" / "provision.sh").read_text())


class TimeZone(unittest.TestCase):
    def unit(self, files: dict, timedatectl: str | None = None) -> str:
        with tempfile.TemporaryDirectory() as d:
            bin_dir = Path(d, "bin")
            bin_dir.mkdir()
            fakes = {"getent": 'echo "wildnetwork:x:1000:1000::/home/wildnetwork:/bin/bash"', "id": "echo 1000"}
            if timedatectl is not None:
                fakes["timedatectl"] = f'echo "{timedatectl}"'
            else:
                fakes["timedatectl"] = "exit 1"
            for name, body in fakes.items():
                Path(bin_dir, name).write_text(f"#!/bin/sh\n{body}\n")
                Path(bin_dir, name).chmod(0o755)
            zi = Path(d, "zoneinfo")
            for z in ("Etc/UTC", "Asia/Kolkata", "Europe/London"):
                Path(zi, z).parent.mkdir(parents=True, exist_ok=True)
                Path(zi, z).touch()
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "WN_ZONEINFO": str(zi),
                   "WN_LOCALTIME": f"{d}/localtime", "WN_ETC_TIMEZONE": f"{d}/timezone"}
            if "localtime" in files:
                os.symlink(f"{zi}/{files['localtime']}", f"{d}/localtime")
            if "timezone" in files:
                Path(d, "timezone").write_text(files["timezone"] + "\n")
            r = subprocess.run(["bash", str(ROOT / "base" / "birdnet-go-unit.sh"), "wildnetwork", "img:1"],
                               capture_output=True, text=True, env=env, check=True)
            return r.stdout

    def test_unit_takes_the_host_zone(self):
        # Debian 13: /etc/timezone can be stale; timedatectl and /etc/localtime are what the host uses
        self.assertIn('--env TZ="Asia/Kolkata"', self.unit({"timezone": "Etc/UTC"}, timedatectl="Asia/Kolkata"))
        self.assertIn('--env TZ="Asia/Kolkata"', self.unit({"localtime": "Asia/Kolkata", "timezone": "Etc/UTC"}))
        self.assertIn('--env TZ="Europe/London"', self.unit({"timezone": "Europe/London"}))
        self.assertIn('--env TZ="UTC"', self.unit({}, timedatectl="Not/AZone"))

    def test_installers_leave_the_time_zone_alone(self):
        for f in ("install.sh", "base/provision.sh", "base/firstboot.sh", "bootstrap.sh",
                  "image/stage-wildnetwork/00-wildnetwork/00-run.sh", "image/stage-wildnetwork/01-birdnet-go/00-run.sh"):
            text = (ROOT / f).read_text()
            for bad in ("set-timezone", "/etc/localtime", "/etc/timezone", "dpkg-reconfigure tzdata"):
                self.assertNotIn(bad, text, f)
        # wnbase.ini (with [base] timezone) is only written when missing
        prov = (ROOT / "base" / "provision.sh").read_text()
        self.assertIn("if [ ! -f /etc/wnbase/wnbase.ini ]; then", prov)
        self.assertIn("[base]\n# Time zone", prov.replace("\r", ""))
        # the image's UTC is only the default of a new card
        build = (ROOT / "image" / "build.sh").read_text()
        self.assertEqual([l for l in build.splitlines() if "TIMEZONE" in l and not l.startswith("#")],
                         ["TIMEZONE_DEFAULT=UTC"])


class Journal(unittest.TestCase):
    def test_persistent_and_capped(self):
        conf = (ROOT / "systemd" / "journald.conf.d" / "wnbase.conf").read_text()
        self.assertIn("Storage=persistent", conf)
        self.assertIn("SystemMaxUse=200M", conf)
        self.assertIn("journald.conf.d/wnbase.conf", (ROOT / "base" / "provision.sh").read_text())


if __name__ == "__main__":
    unittest.main()
