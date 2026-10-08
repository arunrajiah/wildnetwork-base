import configparser
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "base"))
import wnbase  # noqa: E402

AGENT = os.environ.get("WDX_AGENT_PY", str(ROOT.parent / "wdx-agent" / "wdx_agent.py"))


def make_db(path: str) -> None:
    """BirdNET-Go current datastore, same fixture as wdx-agent's BirdNetGoV2 test, with recent timestamps."""
    now = int(time.time())
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE ai_models (id INTEGER PRIMARY KEY, name TEXT, version TEXT);"
        "CREATE TABLE label_types (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE labels (id INTEGER PRIMARY KEY, scientific_name TEXT, model_id INTEGER, label_type_id INTEGER);"
        "CREATE TABLE detections (id INTEGER PRIMARY KEY, model_id INTEGER, label_id INTEGER, detected_at INTEGER, confidence REAL,"
        " latitude REAL, longitude REAL, clip_name TEXT, unlikely NUMERIC DEFAULT 0, legacy_id INTEGER);"
        "INSERT INTO ai_models VALUES (1, 'BirdNET', '2.4');"
        "INSERT INTO label_types VALUES (1, 'species'), (2, 'noise');"
        "INSERT INTO labels VALUES (1, 'Turdus migratorius', 1, 1), (2, 'Engine', 1, 2);"
    )
    con.executemany("INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (1, 1, 1, now - 3600, 0.9, 42.36, -71.06, "clips/2026/10/a.wav", 0, 77),
        (2, 1, 2, now - 3000, 0.95, 42.36, -71.06, None, 0, None),
        (3, 1, 1, now - 2000, 0.9, 42.36, -71.06, None, 1, None),
        (4, 1, 1, now - 1000, 0.92, 42.36, -71.06, None, 0, None),
    ])
    con.commit()
    con.close()


SAMPLE_YAML = (
    "# BirdNET-Go configuration\n"
    "main:\n"
    "    name: BirdNET-Go\n"
    "    latitude: 1.0 # not this one\n"
    "birdnet:\n"
    "    debug: false\n"
    "    # latitude: 99.0 (commented out, must stay)\n"
    "    sensitivity: 1\n"
    "    rangefilter:\n"
    "        latitude: 5.5\n"
    "        threshold: 0.01\n"
    "    latitude: 00.000  # set by the installer\r\n"
    "    longitude: 00.000\n"
    "    locale: en\n"
    "realtime:\n"
    "    weather:\n"
    "        latitude: 7.0\n"
    "        longitude: 8.0\n"
)
EXPECTED_YAML = (SAMPLE_YAML.replace("latitude: 00.000  # set", "latitude: 51.501234  # set")
                 .replace("    longitude: 00.000\n", "    longitude: -0.141234\n"))


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        db = f"{self.tmp}/data/birdnet.db"
        os.makedirs(f"{self.tmp}/data/clips/2026/10")
        Path(f"{self.tmp}/data/clips/2026/10/a.wav").write_bytes(b"RIFF0000WAVEdata")
        Path(f"{self.tmp}/secret.wav").write_bytes(b"nope")
        make_db(db)
        self.ini = f"{self.tmp}/wdx-agent.ini"
        Path(self.ini).write_text(
            "[agent]\nendpoint = http://localhost:9/api/v1/events\napi_key = wn_secret_key\nsource = birdnet-go\n"
            f"path = {db}\nstation_id = station1\nstate_file = {self.tmp}/state.json\n"
            "latitude = 42.36\nlongitude = -71.06\ncustom_key = keep me\n")
        self.calls = []
        os.makedirs(f"{self.tmp}/config")
        self.yaml = f"{self.tmp}/config/config.yaml"
        Path(self.yaml).write_bytes(SAMPLE_YAML.encode())
        os.chmod(self.yaml, 0o640)
        cfg = wnbase.Config(None, agent_config=self.ini, agent_module=AGENT,
                            setup_code_file=f"{self.tmp}/wnbase/setup-code")
        self.app = wnbase.App(cfg, runner=self.fake_run)
        self.srv = wnbase.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def fake_run(self, args, timeout=30):
        self.calls.append(args)
        if args[:2] == ["docker", "restart"]:
            return 0, ""
        return 127, ""  # nmcli, mmcli, docker and systemctl are absent in tests

    def req(self, method, path, body=None, code=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"content-type": "application/json"}
        if code is not None:
            headers["X-Setup-Code"] = code
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        ctype = r.getheader("content-type", "")
        return r.status, (json.loads(data) if ctype.startswith("application/json") else data), r

    def test_info_without_code(self):
        st, info, r = self.req("GET", "/api/info")
        self.assertEqual(st, 200)
        self.assertEqual(info["model"], "wildnetwork-base")
        self.assertTrue(info["configured"])
        self.assertEqual(info["latitude"], 42.36)
        self.assertEqual(r.getheader("access-control-allow-origin"), "*")
        self.assertNotIn("wn_secret_key", json.dumps(info))

    def test_setup_code_file_is_private(self):
        code = Path(self.app.cfg.setup_code_file).read_text().strip()
        self.assertEqual(code, self.app.setup_code)
        self.assertEqual(len(code), 8)
        self.assertEqual(os.stat(self.app.cfg.setup_code_file).st_mode & 0o777, 0o600)

    def test_config_needs_the_code_and_writes_ini(self):
        st, res, _ = self.req("POST", "/api/config", {"name": "Hill"}, code="WRONG123")
        self.assertEqual(st, 403)
        st, res, _ = self.req("POST", "/api/config", {"name": "Hill"})
        self.assertEqual(st, 403)
        body = {"name": "Hill top", "latitude": 51.5, "longitude": -0.12, "roundCoords": 1,
                "apiKey": "wn_new_key", "deviceId": "wnb_1", "apn": "internet"}
        st, res, _ = self.req("POST", "/api/config", body, code=self.app.setup_code.lower())
        self.assertEqual(st, 200, res)
        self.assertTrue(res["ok"] and res["configured"])
        self.assertNotIn("wn_new_key", json.dumps(res))
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(self.ini)
        a = cp["agent"]
        self.assertEqual((a["station_name"], a["latitude"], a["round_coords"], a["api_key"], a["device_id"]),
                         ("Hill top", "51.5", "1", "wn_new_key", "wnb_1"))
        self.assertEqual(a["custom_key"], "keep me")
        self.assertEqual(a["system"], "wildnetwork-base")  # the device key only accepts this system name
        self.assertIn(["systemctl", "restart", "wdx-agent"], self.calls)
        self.assertTrue(any(c[:3] == ["nmcli", "connection", "add"] and "wn-4g" in c for c in self.calls))
        st, info, _ = self.req("GET", "/api/info")
        self.assertEqual((info["name"], info["deviceId"]), ("Hill top", "wnb_1"))
        self.assertNotIn("wn_new_key", json.dumps(info))

    def test_location_goes_into_birdnet_go_config(self):
        st, res, _ = self.req("POST", "/api/config", {"latitude": 51.501234, "longitude": -0.141234, "roundCoords": 2},
                              code=self.app.setup_code)
        self.assertEqual(st, 200, res)
        self.assertNotIn("notes", res)
        self.assertEqual(Path(self.yaml).read_bytes(), EXPECTED_YAML.encode())  # exact, only two lines changed
        self.assertEqual(os.stat(self.yaml).st_mode & 0o777, 0o640)
        self.assertIn(["docker", "restart", "birdnet-go"], self.calls)
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(self.ini)
        self.assertEqual(cp["agent"]["latitude"], "51.501234")  # wdx-agent rounds when sending, not here

    def test_missing_birdnet_go_config_is_a_note(self):
        os.remove(self.yaml)
        st, res, _ = self.req("POST", "/api/config", {"latitude": 10, "longitude": 20}, code=self.app.setup_code)
        self.assertEqual(st, 200, res)
        self.assertIn("BirdNET-Go config not found", res["notes"][0])
        self.assertNotIn(["docker", "restart", "birdnet-go"], self.calls)
        st, res, _ = self.req("POST", "/api/config", {"name": "x"}, code=self.app.setup_code)
        self.assertNotIn("notes", res)

    def test_config_rejects_bad_values(self):
        st, _, _ = self.req("POST", "/api/config", {"latitude": 123}, code=self.app.setup_code)
        self.assertEqual(st, 400)
        st, _, _ = self.req("POST", "/api/config", {"apiKey": "a\nsource = csv"}, code=self.app.setup_code)
        self.assertEqual(st, 400)

    def test_wrong_codes_are_rate_limited(self):
        for _ in range(wnbase.FAIL_LIMIT):
            self.assertEqual(self.req("POST", "/api/ack", {"cursor": 1}, code="BAD")[0], 403)
        self.assertEqual(self.req("POST", "/api/ack", {"cursor": 1}, code=self.app.setup_code)[0], 429)

    def test_pickup_and_ack(self):
        st, res, _ = self.req("GET", "/api/detections?limit=50")
        self.assertEqual(st, 200)
        self.assertEqual(res["cursor"], {"d": 4})
        self.assertEqual([e["eventId"] for e in res["events"]], ["birdnet-go:station1:77", "birdnet-go:station1:d4"])
        st, _, _ = self.req("POST", "/api/ack", {"cursor": {"d": 99}}, code=self.app.setup_code)
        self.assertEqual(st, 409)
        st, ack, _ = self.req("POST", "/api/ack", {"cursor": res["cursor"]}, code=self.app.setup_code)
        self.assertEqual((st, ack), (200, {"ok": True, "advanced": True}))
        st, res, _ = self.req("GET", "/api/detections")
        self.assertEqual((res["cursor"], res["events"]), (None, []))

    def test_limit_moves_cursor_only_past_returned_events(self):
        st, res, _ = self.req("GET", "/api/detections?limit=1")
        self.assertEqual((len(res["events"]), res["cursor"]), (1, {"d": 3}))  # skipped rows 2 and 3 ride along

    def test_status(self):
        st, res, _ = self.req("GET", "/api/status")
        self.assertEqual(st, 200)
        self.assertEqual((res["kind"], res["queue"]["pending"]), ("device-status", 2))
        self.assertIn("network", res)

    def test_recent(self):
        st, res, _ = self.req("GET", "/api/recent?hours=24")
        self.assertEqual(st, 200)
        rows = res["detections"]
        self.assertEqual([r["id"] for r in rows], [4, 3, 1])  # newest first, the noise label left out
        self.assertEqual(rows[-1]["clip"], "a.wav")

    def test_clips(self):
        st, data, r = self.req("GET", "/clips/a.wav")
        self.assertEqual((st, data, r.getheader("content-type")), (200, b"RIFF0000WAVEdata", "audio/wav"))
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/clips/a.wav", headers={"Range": "bytes=4-7"})
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (206, b"0000"))
        c.close()
        for bad in ("/clips/..%2Fetc%2Fpasswd", "/clips/..%2Fsecret.wav", "/clips/../secret.wav",
                    "/clips/%2E%2E%2F%2E%2E%2Fsecret.wav", "/clips/.hidden.wav"):
            st, _, _ = self.req("GET", bad)
            self.assertIn(st, (400, 404), bad)
        self.assertEqual(self.req("GET", "/clips/missing.wav")[0], 404)

    def test_dashboard(self):
        st, data, r = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertTrue(r.getheader("content-type").startswith("text/html"))
        self.assertIn(b"WildNetwork Base", data)
        local = data.replace(b"http://10.42.0.1", b"").replace(b"http://www.w3.org/2000/svg", b"")  # SVG namespace, not a URL to load
        self.assertNotIn(b"http://", local)  # nothing loaded from elsewhere
        self.assertNotIn(b"https://", local)
        self.assertNotIn("\u2014".encode(), data)

    def test_dashboard_carries_the_logo(self):
        logo = (ROOT / "assets" / "logo.svg").read_text()
        self.assertEqual(wnbase.LOGO_SVG, logo)
        st, data, _ = self.req("GET", "/")
        html = data.decode()
        self.assertIn("<title>WildNetwork Base</title>", html)
        self.assertIn(logo, html)
        self.assertIn('rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg', html)
        href = html.split('rel="icon" type="image/svg+xml" href="', 1)[1].split('"', 1)[0]
        self.assertNotIn("#", href)

    def test_unknown_path(self):
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
