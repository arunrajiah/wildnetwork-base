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
        self.assertIn('cp -R "$ROOT/base" "$ROOT/systemd" "$ROOT/install.sh"', build)
        self.assertIn("sha256sum", build)

    def test_release_attaches_checksum(self):
        wf = (ROOT / ".github" / "workflows" / "image.yml").read_text()
        self.assertIn('tags: ["v*"]', wf)
        self.assertIn("image/deploy/*.img.xz.sha256", wf.split("Attach to the release", 1)[1])


if __name__ == "__main__":
    unittest.main()
