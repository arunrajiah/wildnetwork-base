"""Network fallback (base/wnnet.py) against a simulated NetworkManager: power cuts, routers, phones."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "base"))
import wnnet  # noqa: E402

LOG = []
wnnet.log = LOG.append   # keep test output quiet; the messages are checked where they matter


class World:
    """A fake nmcli/iw. NetworkManager autoconnects a client profile `nm_delay` seconds after its network is in
    range, unless the hotspot holds the radio (the bug: an AP profile with autoconnect would win at boot)."""

    def __init__(self, profiles=None, in_range=True, nm_delay=12):
        self.t = 0.0
        self.profiles = dict(profiles if profiles is not None else {"netplan-wlan0-Home": "Home"})
        self.auto = {n: "yes" for n in self.profiles}
        self.in_range = in_range
        self.nm_delay = nm_delay
        self.active = None          # profile holding wlan0
        self.hotspot_profile = None  # its autoconnect value once created
        self.stations = 0
        self.lost_at = 0.0          # when the network last became reachable or the link dropped
        self.log = []

    def set_range(self, ok):
        self.in_range = ok
        self.lost_at = self.t
        if not ok and self.active not in (None, wnnet.HOTSPOT):
            self.active = None

    def nm_autoconnect(self):
        if self.active is None and self.in_range and self.t - self.lost_at >= self.nm_delay:
            for n in self.profiles:
                if self.auto[n] == "yes":
                    self.active = n
                    return
        if self.active is None and self.hotspot_profile == "yes":
            self.active = wnnet.HOTSPOT   # the old bug: the AP takes the radio

    def run(self, args, timeout=30):
        self.log.append(args)
        self.nm_autoconnect()
        if args[:2] == ["nmcli", "-t"] and "device" in args and "status" in args:
            con = self.active or "--"
            return 0, f"wlan0:{'connected' if self.active else 'disconnected'}:{con}\neth0:unavailable:--\n"
        if args[:5] == ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection"]:
            rows = [f"{n}:802-11-wireless:" for n in self.profiles]
            if self.hotspot_profile:
                rows.append(f"{wnnet.HOTSPOT}:802-11-wireless:")
            rows.append("wn-4g:gsm:")
            return 0, "\n".join(rows) + "\n"
        if args[:2] == ["nmcli", "-g"]:
            field, name = args[2], args[-1]
            if name == wnnet.HOTSPOT:
                return (0, wnnet.HOTSPOT + "\n") if self.hotspot_profile else (10, "")
            if name not in self.profiles:
                return 10, "no such connection"
            return 0, {"connection.interface-name": "wlan0", "802-11-wireless.ssid": self.profiles[name],
                       "connection.autoconnect": self.auto[name], "connection.id": name}.get(field, "") + "\n"
        if args[:3] == ["nmcli", "connection", "add"]:
            name = args[args.index("con-name") + 1]
            vals = dict(zip(args[::1], args[1::1]))
            if name == wnnet.HOTSPOT:
                self.hotspot_profile = vals.get("connection.autoconnect", "yes")
            else:
                self.profiles[name] = vals["802-11-wireless.ssid"]
                self.auto[name] = vals.get("connection.autoconnect", "yes")
            return 0, ""
        if args[:3] == ["nmcli", "connection", "modify"]:
            name = args[3]
            vals = dict(zip(args[4::2], args[5::2]))
            if name == wnnet.HOTSPOT:
                self.hotspot_profile = vals.get("connection.autoconnect", self.hotspot_profile)
            elif name in self.profiles:
                self.profiles[name] = vals.get("802-11-wireless.ssid", self.profiles[name])
                self.auto[name] = vals.get("connection.autoconnect", self.auto[name])
            return 0, ""
        if "up" in args and "connection" in args:
            name = args[-1]
            if name == wnnet.HOTSPOT:
                self.active = name
                return 0, ""
            if self.in_range and name in self.profiles:
                self.active = name
                self.t += 8
                return 0, ""
            self.t += wnnet.CONNECT_WAIT
            return 4, "Error: Connection activation failed: no network with that SSID"
        if args[:3] == ["nmcli", "connection", "down"]:
            if self.active == args[3]:
                self.active = None
                self.lost_at = self.t
            return 0, ""
        if args[:4] == ["nmcli", "-t", "-f", "SSID"]:
            return 0, ("Home\nNeighbour\n" if self.in_range else "Neighbour\n")
        if args[:2] == ["iw", "dev"]:
            return 0, "".join(f"Station aa:bb:cc:dd:ee:0{i} (on wlan0)\n" for i in range(self.stations))
        return 0, ""

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t


def make(world, **kw):
    s = wnnet.Settings(None, **kw)
    s.ssid, s.psk = "Wildlife Network-AB12", "wn-ABCD2345"
    return wnnet.Net(s, run=world.run, now=world.now, sleep=world.sleep, boot_dir=tempfile.mkdtemp())


def run_for(net, world, seconds):
    end = world.t + seconds
    while world.t < end:
        world.sleep(wnnet.CHECK_EVERY)
        net.tick()


class PowerCut(unittest.TestCase):
    def test_hotspot_profile_never_autoconnects(self):
        w = World()
        w.hotspot_profile = "yes"            # a Base set up before this fix
        make(w).ensure_hotspot_profile()
        self.assertEqual(w.hotspot_profile, "no")

    def test_boot_with_home_wifi_available(self):
        w = World()
        net = make(w)
        net.boot()
        self.assertEqual(w.active, "netplan-wlan0-Home")
        self.assertEqual(net.mode, "client")
        self.assertLess(w.t, 30)
        run_for(net, w, 3600)
        self.assertEqual(w.active, "netplan-wlan0-Home")   # and stays there

    def test_five_power_cuts_in_a_row(self):
        for _ in range(5):
            w = World(nm_delay=25)               # slow scan after a cold start
            w.hotspot_profile = "no"
            net = make(w)
            net.boot()
            self.assertEqual(w.active, "netplan-wlan0-Home")

    def test_boot_without_home_wifi_then_it_appears(self):
        w = World(in_range=False)
        net = make(w)
        net.boot()
        self.assertEqual(w.active, wnnet.HOTSPOT)
        self.assertGreaterEqual(w.t, 90)
        run_for(net, w, 600)
        self.assertEqual(w.active, wnnet.HOTSPOT)            # still away: hotspot stays
        w.set_range(True)                                   # the router is back
        run_for(net, w, 320)
        self.assertEqual(w.active, "netplan-wlan0-Home")
        self.assertEqual(net.mode, "client")

    def test_no_saved_wifi_starts_hotspot_at_once(self):
        w = World(profiles={})
        net = make(w)
        net.boot()
        self.assertEqual(w.active, wnnet.HOTSPOT)
        self.assertLess(w.t, 5)

    def test_router_restart_during_normal_running(self):
        w = World()
        net = make(w)
        net.boot()
        w.set_range(False)                                  # router restarts for 2 minutes
        run_for(net, w, 120)
        self.assertIsNone(w.active)                         # waited, no hotspot yet
        w.set_range(True)
        run_for(net, w, 60)
        self.assertEqual(w.active, "netplan-wlan0-Home")
        self.assertFalse(any(a[-1] == wnnet.HOTSPOT and "up" in a for a in w.log))

    def test_router_gone_for_long_falls_back(self):
        w = World()
        net = make(w)
        net.boot()
        w.set_range(False)
        run_for(net, w, 400)
        self.assertEqual(w.active, wnnet.HOTSPOT)

    def test_phone_on_hotspot_is_not_interrupted(self):
        w = World(in_range=False)
        net = make(w)
        net.boot()
        w.stations = 1                                      # someone is setting up the Base
        w.set_range(True)
        run_for(net, w, 1800)
        self.assertEqual(w.active, wnnet.HOTSPOT)
        self.assertFalse(any(a[:3] == ["nmcli", "connection", "down"] for a in w.log))
        w.stations = 0                                      # setup done, phone left
        run_for(net, w, 320)
        self.assertEqual(w.active, "netplan-wlan0-Home")

    def test_force_keeps_hotspot(self):
        w = World()
        net = make(w, force=True)
        net.boot()
        self.assertEqual(w.active, wnnet.HOTSPOT)
        run_for(net, w, 900)
        self.assertEqual(w.active, wnnet.HOTSPOT)


class OneHomeWifi(unittest.TestCase):
    def test_save_makes_one_managed_profile(self):
        # what the desk-test Base had: Imager's netplan profile plus an older wn-uplink with another SSID variant
        w = World(profiles={"netplan-wlan0-Home": "Home", "wn-uplink": "Home-5G", "Cafe": "Cafe"})
        warnings = wnnet.save_uplink(w.run, "wlan0", "Home", "secret123")
        self.assertEqual(warnings, [])
        self.assertEqual(w.profiles["wn-uplink"], "Home")
        self.assertEqual(w.auto, {"netplan-wlan0-Home": "no", "wn-uplink": "yes", "Cafe": "no"})
        self.assertFalse(any("wn-4g" in a for a in w.log if a[:3] == ["nmcli", "connection", "modify"]))
        net = make(w)
        net.boot()
        self.assertEqual(w.active, "wn-uplink")

    def test_save_creates_the_profile(self):
        w = World(profiles={"netplan-wlan0-Home": "Home"})
        wnnet.save_uplink(w.run, "wlan0", "Farm", "secret123")
        self.assertEqual(w.profiles["wn-uplink"], "Farm")
        self.assertEqual(w.auto["netplan-wlan0-Home"], "no")
        self.assertEqual(wnnet.client_profiles(w.run, "wlan0")[0], "wn-uplink")


class WifiFromCard(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(wnnet.read_wifi_file("\ufeff# home\nSSID = My Home\r\npassword=\"pa ss=word\"\ncountry: in\nfoo=bar\n"),
                         {"ssid": "My Home", "password": "pa ss=word", "country": "in"})

    def test_applied_at_boot_and_password_removed(self):
        w = World(profiles={"netplan-wlan0-Old": "Old"}, in_range=False)
        net = make(w)
        card = Path(net.boot_dir, "wildnetwork-wifi.txt")
        card.write_text("ssid=Home\npassword=correct-horse\ncountry=IN\n")
        w.in_range = True
        net.boot()
        self.assertEqual(w.profiles["wn-uplink"], "Home")
        self.assertEqual(w.auto["netplan-wlan0-Old"], "no")
        self.assertIn(["raspi-config", "nonint", "do_wifi_country", "IN"], w.log)
        self.assertEqual(w.active, "wn-uplink")
        self.assertFalse(card.exists())
        applied = Path(net.boot_dir, "wildnetwork-wifi.txt.applied").read_text()
        self.assertIn("ssid=Home", applied)
        self.assertNotIn("correct-horse", applied)

    def test_bad_file_is_not_applied_but_still_removed(self):
        w = World()
        net = make(w)
        card = Path(net.boot_dir, "wildnetwork-wifi.txt")
        card.write_text("ssid=Home\npassword=short\n")
        net.apply_wifi_file()
        self.assertNotIn("wn-uplink", w.profiles)
        self.assertFalse(card.exists())
        self.assertIn("not applied", Path(net.boot_dir, "wildnetwork-wifi.txt.applied").read_text())


class Install(unittest.TestCase):
    def test_service_replaces_the_hotspot_timer(self):
        prov = (ROOT / "base" / "provision.sh").read_text()
        self.assertIn("UNITS=(wnbase.service wn-net.service wdx-agent.service)", prov)
        self.assertIn("for u in wn-hotspot.timer wn-hotspot.service; do", prov)   # removed on re-runs
        self.assertIn("wnnet.py", prov)
        unit = (ROOT / "systemd" / "wn-net.service").read_text()
        self.assertIn("SyslogIdentifier=wn-net", unit)
        self.assertFalse((ROOT / "systemd" / "wn-hotspot.timer").exists())
        self.assertNotIn("autoconnect yes", (ROOT / "base" / "wnnet.py").read_text().split("def ensure_hotspot_profile")[1]
                         .split("def hotspot_up")[0])

    def test_settings_from_ini(self):
        d = tempfile.mkdtemp()
        Path(d, "wnbase.ini").write_text("[hotspot]\nboot_wait_seconds = 120\nretry_seconds = 600\nforce = yes\n")
        s = wnnet.Settings(str(Path(d, "wnbase.ini")))
        self.assertEqual((s.boot_wait, s.retry, s.fallback, s.force), (120, 600, 180, True))


if __name__ == "__main__":
    unittest.main()
