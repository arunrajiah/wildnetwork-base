"""Time zone of the Base: offline lookup from the location, BirdNET-Go's unit, and when it is applied."""
import configparser
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "base"))
import tzgrid  # noqa: E402
import wnbase  # noqa: E402

AGENT = os.environ.get("WDX_AGENT_PY", str(ROOT.parent / "wdx-agent" / "wdx_agent.py"))

UNIT = """[Service]
ExecStart=/usr/bin/docker run --rm \\
    --name birdnet-go \\
    -p 8080:8080 \\
    --env TZ="Etc/UTC" \\
    --env BIRDNET_UID=1000 \\
    ghcr.io/tphakala/birdnet-go:20260823
"""


class Lookup(unittest.TestCase):
    """The bundled grid (base/tzgrid.dat), as on the Base."""

    def test_places(self):
        for lat, lon, zone in [
            (13.0827, 80.2707, "Asia/Kolkata"),      # Chennai
            (13.05, 80.285, "Asia/Kolkata"),         # Chennai, on the beach (cell centre at sea)
            (12.9716, 77.5946, "Asia/Kolkata"),      # Bengaluru
            (8.0883, 77.5385, "Asia/Kolkata"),       # Kanyakumari
            (27.7172, 85.3240, "Asia/Kathmandu"),    # +05:45
            (51.5072, -0.1276, "Europe/London"),
            (40.7128, -74.0060, "America/New_York"),
            (-33.8688, 151.2093, "Australia/Sydney"),
            (-1.2921, 36.8219, "Africa/Nairobi"),
            (52.5200, 13.4050, "Europe/Berlin"),
        ]:
            self.assertEqual(tzgrid.timezone_at(lat, lon), zone, (lat, lon))

    def test_open_sea_and_edges(self):
        self.assertEqual(tzgrid.timezone_at(0, -30), "Etc/GMT+2")
        for lat, lon in [(90, 180), (-90, -180), (0, 180), (0, -180)]:
            self.assertTrue(wnbase.valid_zone(tzgrid.timezone_at(lat, lon)), (lat, lon))
        with self.assertRaises(ValueError):
            tzgrid.timezone_at(91, 0)

    def test_every_zone_is_known_to_this_system(self):
        grid = tzgrid.TzGrid()
        unknown = [z for z in grid.zones if not wnbase.valid_zone(z)]
        self.assertEqual(unknown, [])

    @unittest.skipUnless(__import__("importlib").util.find_spec("timezonefinder"), "timezonefinder not installed")
    def test_agrees_with_timezonefinder(self):
        from timezonefinder import TimezoneFinder
        tf = TimezoneFinder(in_memory=True)
        rnd = random.Random(7)
        n = same = 0
        while n < 20000:
            lat, lon = rnd.uniform(-56, 72), rnd.uniform(-180, 180)
            z = tf.timezone_at_land(lat=lat, lng=lon)
            if z:
                n += 1
                same += tzgrid.timezone_at(lat, lon) == z
        self.assertGreater(same / n, 0.99)


class UnitRewrite(unittest.TestCase):
    def test_read_and_set(self):
        self.assertEqual(wnbase.unit_zone(UNIT), "Etc/UTC")
        new = wnbase.set_unit_zone(UNIT, "Asia/Kolkata")
        self.assertIn('--env TZ="Asia/Kolkata" \\\n', new)
        self.assertEqual(new.replace("Asia/Kolkata", "Etc/UTC"), UNIT)   # nothing else changed
        self.assertEqual(wnbase.unit_zone("docker run -e TZ=UTC x"), "UTC")
        self.assertIsNone(wnbase.unit_zone("docker run x"))

    def test_valid_zone(self):
        self.assertEqual(wnbase.valid_zone("Asia/Kolkata"), "Asia/Kolkata")
        for bad in ("", "Mars/Olympus", "../etc/passwd", "Asia/Kolkata; rm", None, 5):
            self.assertIsNone(wnbase.valid_zone(bad), bad)


class Base(unittest.TestCase):
    """A Base at Chennai still on UTC (system and BirdNET-Go)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.zoneinfo = Path(self.tmp, "zoneinfo")
        self.localtime = Path(self.tmp, "localtime")
        self.point("Etc/UTC")
        self.unit = Path(self.tmp, "birdnet-go.service")
        self.unit.write_text(UNIT)
        self.ini = Path(self.tmp, "wdx-agent.ini")
        self.ini.write_text(f"[agent]\nsource = birdnet-go\npath = {self.tmp}/data/birdnet.db\nstation_id = s1\n"
                            f"state_file = {self.tmp}/state.json\nlatitude = 13.0827\nlongitude = 80.2707\n")
        self.calls, self.process_zone = [], None
        self.app = self.make_app()

    def make_app(self, **cfg):
        app = wnbase.App(wnbase.Config(None, agent_config=str(self.ini), agent_module=AGENT,
                                       setup_code_file=f"{self.tmp}/code", state_dir=f"{self.tmp}/state",
                                       birdnet_unit=str(self.unit), birdnet_unit_lib=f"{self.tmp}/none",
                                       localtime=str(self.localtime), etc_timezone=f"{self.tmp}/timezone",
                                       **cfg), runner=self.fake_run)
        app.use_zone_in_process = lambda z: setattr(self, "process_zone", z)
        return app

    def point(self, zone):
        f = self.zoneinfo / zone
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
        if self.localtime.is_symlink():
            self.localtime.unlink()
        self.localtime.symlink_to(f)

    def fake_run(self, args, timeout=30):
        self.calls.append(args)
        if args[:2] == ["timedatectl", "set-timezone"]:
            self.point(args[2])
            return 0, ""
        if args[0] == "systemctl":
            return 0, ""
        return 127, ""

    def test_applies_the_zone_of_the_location(self):
        res = self.app.apply_timezone("test")
        self.assertEqual((res["zone"], res["source"], res["changed"], res["warnings"]),
                         ("Asia/Kolkata", "location", True, []))
        self.assertEqual(self.app.system_zone(), "Asia/Kolkata")
        self.assertEqual(wnbase.unit_zone(self.unit.read_text()), "Asia/Kolkata")
        self.assertEqual(self.process_zone, "Asia/Kolkata")
        self.assertEqual(self.calls, [["timedatectl", "set-timezone", "Asia/Kolkata"],
                                      ["systemctl", "stop", "birdnet-go"], ["systemctl", "daemon-reload"],
                                      ["systemctl", "start", "birdnet-go"], ["systemctl", "restart", "wdx-agent"]])
        self.calls.clear()
        res = self.app.apply_timezone("again")   # nothing to do the second time
        self.assertFalse(res["changed"])
        self.assertEqual(self.calls, [])

    def test_saving_a_location_sets_the_zone(self):
        res = self.app.configure({"latitude": 51.5072, "longitude": -0.1276})
        self.assertEqual(res["timezone"], "Europe/London")
        self.assertEqual(self.app.system_zone(), "Europe/London")
        self.assertEqual(wnbase.unit_zone(self.unit.read_text()), "Europe/London")
        self.assertNotIn(["docker", "restart", "birdnet-go"], self.calls)   # restarted once, by systemctl

    def test_no_location_no_change(self):
        self.ini.write_text(f"[agent]\nsource = birdnet-go\npath = {self.tmp}/db\nstate_file = {self.tmp}/s.json\n")
        res = self.app.apply_timezone("test")
        self.assertEqual((res["zone"], res["changed"]), (None, False))
        self.assertEqual(self.calls, [])

    def test_without_birdnet_go_only_the_system(self):
        self.unit.unlink()
        res = self.app.apply_timezone("test")
        self.assertTrue(res["changed"])
        self.assertEqual(self.calls[0], ["timedatectl", "set-timezone", "Asia/Kolkata"])
        self.assertNotIn(["systemctl", "stop", "birdnet-go"], self.calls)

    def test_unit_only_in_lib_is_copied_to_etc(self):
        lib = Path(self.tmp, "lib.service")
        lib.write_text(UNIT)
        self.unit.unlink()
        app = self.make_app()
        app.cfg.birdnet_unit_lib = str(lib)
        app.apply_timezone("test")
        self.assertEqual(wnbase.unit_zone(self.unit.read_text()), "Asia/Kolkata")
        self.assertEqual(lib.read_text(), UNIT)


if __name__ == "__main__":
    unittest.main()
