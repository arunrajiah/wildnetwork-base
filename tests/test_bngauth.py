import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "base"))
import bngauth  # noqa: E402

# Shape of BirdNET-Go 20260823's default config: a "security" logger module and the top-level security section
CONFIG = """logging:
  modules:
    security:
      enabled: true
      file_path: logs/security.log
security:
  host: ""
  sessionduration: 168h
  allowsubnetbypass:
    enabled: false           # true to disable OAuth in subnet
  basicauth:
    enabled: false           # true to enable basic auth
    password: ""             # password hash for the settings interface
    clientid: ""             # client id
  googleAuth:
    enabled: false
"""


class BirdnetGoAuth(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = f"{self.d}/config.yaml"
        self.pwf = f"{self.d}/etc/birdnet-go-password"
        Path(self.cfg).write_text(CONFIG)

    def test_turns_on_auth_with_a_random_password(self):
        changed, msg = bngauth.apply(self.cfg, self.pwf)
        self.assertTrue(changed, msg)
        text = Path(self.cfg).read_text()
        pw = Path(self.pwf).read_text().strip()
        self.assertGreaterEqual(len(pw), 20)
        self.assertEqual(os.stat(self.pwf).st_mode & 0o777, 0o600)
        self.assertEqual(bngauth.get(text, ["security", "basicauth", "enabled"]), "true")
        self.assertEqual(bngauth.get(text, ["security", "basicauth", "password"]), pw)
        self.assertIn(f'    password: "{pw}"             # password hash', text)
        # only those two lines changed: the logger module and the other "enabled" keys are untouched
        diff = [a for a, b in zip(CONFIG.splitlines(), text.splitlines()) if a != b]
        self.assertEqual(len(diff), 2)
        self.assertEqual(Path(self.cfg + bngauth.BACKUP_SUFFIX).read_text(), CONFIG)
        changed, msg = bngauth.apply(self.cfg, self.pwf)   # run again: nothing to do
        self.assertFalse(changed)
        self.assertIn("password protected", msg)

    def test_password_is_reused_after_a_config_reset(self):
        bngauth.apply(self.cfg, self.pwf)
        pw = Path(self.pwf).read_text().strip()
        Path(self.cfg).write_text(CONFIG)
        bngauth.apply(self.cfg, self.pwf)
        self.assertEqual(bngauth.get(Path(self.cfg).read_text(), ["security", "basicauth", "password"]), pw)

    def test_owners_password_is_kept(self):
        own = CONFIG.replace('    password: ""', '    password: "mine"').replace("    enabled: false           # true to enable basic auth", "    enabled: true")
        Path(self.cfg).write_text(own)
        changed, msg = bngauth.apply(self.cfg, self.pwf)
        self.assertFalse(changed)
        self.assertIn("left as you set it", msg)
        self.assertEqual(Path(self.cfg).read_text(), own)
        self.assertFalse(os.path.exists(self.pwf))

    def test_config_without_security_section(self):
        Path(self.cfg).write_text("main:\n  name: x\n")
        changed, msg = bngauth.apply(self.cfg, self.pwf)
        self.assertFalse(changed)
        self.assertTrue(msg.startswith("WARNING"))


class WnbasePassword(unittest.TestCase):
    def test_needs_the_setup_code(self):
        import wnbase
        from test_wnbase import AGENT
        d = tempfile.mkdtemp()
        Path(f"{d}/pw").write_text("s3cret-pw\n")
        Path(f"{d}/ini").write_text(f"[agent]\nendpoint = http://localhost:9/x\nsource = birdnet-go\npath = {d}/db\n")
        app = wnbase.App(wnbase.Config(None, agent_config=f"{d}/ini", agent_module=AGENT, setup_code_file=f"{d}/code",
                                       birdnet_password_file=f"{d}/pw"), runner=lambda a, t=30: (127, ""))
        srv = wnbase.make_server(app, "127.0.0.1", 0)
        import http.client
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def post(code):
                c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
                c.request("POST", "/api/birdnet-go-password", "{}", {"content-type": "application/json", "X-Setup-Code": code})
                r = c.getresponse()
                return r.status, json.loads(r.read())
            self.assertEqual(post("WRONG")[0], 403)
            st, body = post(app.setup_code)
            self.assertEqual((st, body["password"]), (200, "s3cret-pw"))
            self.assertNotIn("s3cret-pw", json.dumps(app.info()))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
