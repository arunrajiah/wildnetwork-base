import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "base"))
import wnupdate  # noqa: E402

WIFI = "wn-uplink:802-11-wireless:wlan0\n"
HOTSPOT_ONLY = "wn-hotspot:802-11-wireless:wlan0\nlo:loopback:lo\n"


def tarball(version: str, extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    files = {"base/wnbase.py": f'VERSION = "{version}"\n', "base/wnnet.py": "# same\n",
             "base/provision.sh": "#!/bin/bash\n", **(extra or {})}
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"wildnetwork-base-{version}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Decisions(unittest.TestCase):
    def test_version_comparison(self):
        self.assertTrue(wnupdate.is_newer("v0.3.0", "0.2.2"))
        self.assertTrue(wnupdate.is_newer("0.10.0", "0.9.9"))
        self.assertTrue(wnupdate.is_newer("0.4.7", "0.4.6"))
        self.assertTrue(wnupdate.is_newer("0.3", "0.2.9"))
        self.assertFalse(wnupdate.is_newer("0.3.0", "0.3"))
        self.assertFalse(wnupdate.is_newer("0.2.2", "0.2.2"))
        self.assertFalse(wnupdate.is_newer("0.2.1", "0.2.2"))         # never downgrade
        self.assertFalse(wnupdate.is_newer("0.1.0-beta.1", "0.0.1"))  # pre-releases are not taken
        self.assertFalse(wnupdate.is_newer("garbage", "0.1.0"))
        self.assertTrue(wnupdate.is_newer("0.4.6", None))               # nothing installed yet

    def test_switch_precedence(self):
        self.assertTrue(wnupdate.should_run(""))          # missing or empty: on (the default)
        self.assertTrue(wnupdate.should_run("yes"))
        self.assertFalse(wnupdate.should_run("no"))
        self.assertFalse(wnupdate.should_run(" Off "))
        self.assertFalse(wnupdate.should_run("0"))
        self.assertTrue(wnupdate.should_run("no", force=True))   # run by hand with --now wins over the switch

    def test_rollback_decision(self):
        self.assertTrue(wnupdate.came_up("0.3.0", "0.3.0"))
        self.assertFalse(wnupdate.came_up("0.3.0", "0.2.2"))   # old version still answering
        self.assertFalse(wnupdate.came_up("0.3.0", None))      # not answering at all

    def test_uplink(self):
        self.assertTrue(wnupdate.has_uplink(lambda a, t=30: (0, WIFI + HOTSPOT_ONLY)))
        self.assertTrue(wnupdate.has_uplink(lambda a, t=30: (0, "wn-4g:gsm:cdc-wdm0\n")))
        self.assertFalse(wnupdate.has_uplink(lambda a, t=30: (0, HOTSPOT_ONLY)))
        self.assertFalse(wnupdate.has_uplink(lambda a, t=30: (10, "")))

    def test_release_assets(self):
        rel = {"tag_name": "v0.3.0", "assets": [
            {"name": "wnbase-0.3.0.tar.gz", "browser_download_url": "https://x/t"},
            {"name": "wnbase-0.3.0.tar.gz.sha256", "browser_download_url": "https://x/s"}]}
        self.assertEqual(wnupdate.release_assets(rel), ("0.3.0", "https://x/t", "https://x/s"))
        self.assertEqual(wnupdate.release_assets({"tag_name": "v0.2.2", "assets": []}), ("0.2.2", None, None))
        self.assertEqual(wnupdate.release_assets({**rel, "prerelease": True}), (None, None, None))


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        t = Path(self.tmp)
        (t / "opt").mkdir()
        (t / "opt" / "wnbase.py").write_text('VERSION = "0.2.2"\n')
        (t / "opt" / "wnnet.py").write_text("# same\n")
        (t / "units").mkdir()
        (t / "units" / "wnbase.service").write_text("old unit\n")
        (t / "units" / "wdx-agent.service").write_text("[Service]\nUser=pi\n")
        (t / "agent").mkdir()
        self.agent = t / "agent" / "wdx_agent.py"
        self.agent.write_text('VERSION = "0.4.6"\ndef pending_batch():\n    pass\n')
        (t / "wdx-agent.ini").write_text("[agent]\nendpoint = https://example.org/api/v1/events\n")
        self.ini = t / "wnbase.ini"
        self.ini.write_text(f"[wnbase]\nagent_config = {t}/wdx-agent.ini\nagent_module = {self.agent}\n"
                            f"state_dir = {t}/state\n\n[base]\nauto_update = yes\n")
        self.nm = WIFI
        self.tar = tarball("0.3.0")
        self.sha = hashlib.sha256(self.tar).hexdigest() + "  wnbase-0.3.0.tar.gz\n"
        self.release = {"tag_name": "v0.3.0", "assets": [
            {"name": "wnbase-0.3.0.tar.gz", "browser_download_url": "https://gh/t"},
            {"name": "wnbase-0.3.0.tar.gz.sha256", "browser_download_url": "https://gh/s"}]}
        self.agent_latest = "0.4.6"
        self.agent_file = None
        self.wnbase_reports = None     # None: whatever /opt/wnbase/wnbase.py holds now
        self.agent_journal = ""
        self.agent_active = False
        self.fetched, self.ran = [], []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, args, timeout=60, env=None):
        self.ran.append(args)
        if args[:3] == ["nmcli", "-t", "-f"]:
            return 0, self.nm
        if args[0] == "bash" and args[-1] == "--update":   # stands in for provision.sh --update
            src = Path(env["WN_SRC"], "base")
            for f in src.iterdir():
                if f.suffix == ".py":
                    shutil.copy(f, Path(self.tmp, "opt", f.name))
            Path(self.tmp, "units", "wnbase.service").write_text("new unit\n")
            return 0, ""
        if args[:2] == ["systemctl", "is-active"]:
            return (0 if self.agent_active else 3), ""
        if args[:3] == ["systemctl", "show", "-p"]:
            return 0, "inv1\n"
        if args[0] == "journalctl":
            return 0, self.agent_journal
        return 0, ""

    def fetch(self, url, timeout=60):
        self.fetched.append(url)
        if url == wnupdate.RELEASES_LATEST:
            return json.dumps(self.release).encode()
        if url == "https://gh/t":
            return self.tar
        if url == "https://gh/s":
            return self.sha.encode()
        if url == "https://example.org/agent/VERSION":
            return self.agent_latest.encode() + b"\n"
        if url == "https://example.org/agent/wdx_agent.py":
            return self.agent_file.encode()
        if url.endswith("/api/info"):
            base = wnupdate.file_version(Path(self.tmp, "opt", "wnbase.py").read_text())
            return json.dumps({"software": {
                "wnbase": self.wnbase_reports or base,
                "wdx-agent": wnupdate.file_version(self.agent.read_text())}}).encode()
        raise OSError(f"unexpected {url}")

    def updater(self):
        t = self.tmp
        return wnupdate.Updater(str(self.ini), run=self.run_cmd, fetch=self.fetch, sleep=lambda s: None,
                                opt_dir=f"{t}/opt", prev_dir=f"{t}/opt.prev", unit_dir=f"{t}/units")

    def state(self):
        return json.loads(Path(self.tmp, "state", "update.json").read_text())

    def test_skips_without_uplink(self):
        self.nm = HOTSPOT_ONLY
        st = self.updater().check()
        self.assertIn("no internet", st["lastResult"])
        self.assertEqual(self.fetched, [])
        self.assertIn("lastCheck", self.state())

    def test_skips_when_switched_off(self):
        self.ini.write_text(self.ini.read_text().replace("auto_update = yes", "auto_update = no"))
        st = self.updater().check()
        self.assertIn("switched off", st["lastResult"])
        self.assertEqual(self.fetched, [])
        st = self.updater().check(force=True)            # --now
        self.assertTrue(st["lastResult"].startswith("updated wnbase"), st["lastResult"])

    def test_updates_base(self):
        st = self.updater().check()
        self.assertEqual(st["lastResult"], "updated wnbase 0.2.2 -> 0.3.0")
        self.assertEqual(st["latest"], {"wnbase": "0.3.0", "wdx-agent": "0.4.6"})
        self.assertIn('"0.3.0"', Path(self.tmp, "opt", "wnbase.py").read_text())
        self.assertIn('"0.2.2"', Path(self.tmp, "opt.prev", "wnbase.py").read_text())   # previous kept
        self.assertIn(["systemctl", "restart", "wnbase.service"], self.ran)
        self.assertNotIn(["systemctl", "restart", "wn-net.service"], self.ran)            # wnnet.py unchanged
        n = len(self.fetched)
        self.assertEqual(self.updater().check()["lastResult"], "up to date")
        self.assertNotIn("https://gh/t", self.fetched[n:])

    def test_rolls_back_when_new_version_does_not_come_up(self):
        self.wnbase_reports = "0.2.2"     # the new code never answers as 0.3.0
        st = self.updater().check()
        self.assertTrue(st["lastResult"].startswith("rolled back: wnbase 0.3.0 failed"), st["lastResult"])
        self.assertIn('"0.2.2"', Path(self.tmp, "opt", "wnbase.py").read_text())
        self.assertEqual(Path(self.tmp, "units", "wnbase.service").read_text(), "old unit\n")
        self.assertIn(["systemctl", "daemon-reload"], self.ran)

    def test_bad_checksum_changes_nothing(self):
        self.sha = "0" * 64 + "  wnbase-0.3.0.tar.gz\n"
        st = self.updater().check()
        self.assertIn("does not match its sha256", st["lastResult"])
        self.assertIn('"0.2.2"', Path(self.tmp, "opt", "wnbase.py").read_text())
        self.assertFalse(os.path.exists(Path(self.tmp, "opt.prev")))

    def test_version_inside_must_match_tag(self):
        self.tar = tarball("0.2.9")
        self.sha = hashlib.sha256(self.tar).hexdigest()
        st = self.updater().check()
        self.assertIn("holds version 0.2.9", st["lastResult"])

    def test_release_without_update_file(self):
        self.release["assets"] = []
        st = self.updater().check()
        self.assertIn("has no update files", st["lastResult"])

    def test_updates_agent_and_checks_version_inside(self):
        self.release["tag_name"] = "v0.2.2"
        self.agent_latest = "0.4.7"
        self.agent_file = 'VERSION = "0.4.6"\ndef pending_batch():\n    pass\n'     # VERSION says otherwise
        st = self.updater().check()
        self.assertIn("says 0.4.6, not 0.4.7", st["lastResult"])
        self.agent_file = 'VERSION = "0.4.7"\ndef pending_batch():\n    pass\n'
        self.agent_active = True
        self.agent_journal = "wdx-agent 0.4.7 source=birdnet-go\n"
        st = self.updater().check()
        self.assertEqual(st["lastResult"], "updated wdx-agent 0.4.6 -> 0.4.7")
        self.assertIn('"0.4.7"', self.agent.read_text())
        self.assertIn('"0.4.6"', Path(str(self.agent) + ".prev").read_text())

    def test_agent_rolls_back(self):
        self.release["tag_name"] = "v0.2.2"
        self.agent_latest = "0.4.7"
        self.agent_file = 'VERSION = "0.4.7"\ndef pending_batch():\n    pass\n'
        self.agent_active = True
        self.agent_journal = "Traceback: crashed\n"     # never logs its version
        st = self.updater().check()
        self.assertTrue(st["lastResult"].startswith("rolled back: wdx-agent 0.4.7 failed"), st["lastResult"])
        self.assertIn('"0.4.6"', self.agent.read_text())

    def test_offline_lookup_is_an_error_not_a_change(self):
        self.release = None
        self.fetch_orig = self.fetch

        def failing(url, timeout=60):
            if url == wnupdate.RELEASES_LATEST:
                raise OSError("timed out")
            return self.fetch_orig(url, timeout)
        self.fetch = failing
        st = self.updater().check()
        self.assertTrue(st["lastResult"].startswith("error: newest Base version not found"), st["lastResult"])


class Units(unittest.TestCase):
    def test_timers(self):
        nightly = (ROOT / "systemd" / "wnbase-update.timer").read_text()
        self.assertIn("RandomizedDelaySec=", nightly)
        self.assertIn("OnCalendar=", nightly)
        boot = (ROOT / "systemd" / "wnbase-update-boot.timer").read_text()
        self.assertIn("OnBootSec=10min", boot)
        self.assertNotIn("RandomizedDelaySec=", boot)
        svc = (ROOT / "systemd" / "wnbase-update.service").read_text()
        self.assertIn("SyslogIdentifier=wnbase-update", svc)
        self.assertIn("/opt/wnbase/wnupdate.py", svc)

    def test_provision_installs_and_enables_them(self):
        prov = (ROOT / "base" / "provision.sh").read_text()
        for u in wnupdate.UNITS:
            if u != "wdx-agent.service":
                self.assertIn(u, prov)
        self.assertIn("--update", prov)
        self.assertIn("wnupdate.py", prov)

    def test_release_attaches_update_file(self):
        wf = (ROOT / ".github" / "workflows" / "image.yml").read_text()
        self.assertIn('git archive --format=tar.gz --prefix="wildnetwork-base-$ver/" -o "wnbase-$ver.tar.gz"', wf)
        self.assertIn('sha256sum "wnbase-$ver.tar.gz" > "wnbase-$ver.tar.gz.sha256"', wf)


if __name__ == "__main__":
    unittest.main()
