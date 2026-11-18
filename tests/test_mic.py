import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "base"))
import mic  # noqa: E402
import wnbase  # noqa: E402

AGENT = os.environ.get("WDX_AGENT_PY", str(ROOT.parent / "wdx-agent" / "wdx_agent.py"))

# `arecord -l` on the first real Base: Raspberry Pi 4, MAONO AU-UL10 USB lavalier microphone
PI4_USB = """**** List of CAPTURE Hardware Devices ****
card 1: Device [USB Lavalier Microphone], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""
ONBOARD_ONLY = "**** List of CAPTURE Hardware Devices ****\n"   # Pi onboard audio and HDMI cannot record
HAT_ONLY = """**** List of CAPTURE Hardware Devices ****
card 2: sndrpii2scard [snd_rpi_i2s_card], device 0: simple-card_codec_link snd-soc-dummy-dai-0 [simple-card_codec_link snd-soc-dummy-dai-0]
  Subdevices: 1/1
"""
HAT_AND_USB = HAT_ONLY + """card 3: Mic [Generic Microphone], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
"""

# BirdNET-Go config (current layout): only realtime.audio.sources[0].device may change
CONFIG = """# BirdNET-Go configuration
main:
    name: BirdNET-Go
birdnet:
    latitude: 00.000
realtime:
    interval: 15
    rtsp:
        device: not-this-one
    audio:
        source: ""
        sources:
            - name: Default
              device: sysdefault  # set by the installer
              gain: 0
            - name: Second
              device: sysdefault
        export:
            enabled: true
    weather:
        provider: yrno
"""
LEGACY = """realtime:
  audio:
    source: "sysdefault"
    export:
      path: /data/clips/
"""


class Parse(unittest.TestCase):
    def test_usb_lavalier_on_pi4(self):
        devs = mic.parse_arecord(PI4_USB)
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["alsa"], ":1,0")
        self.assertTrue(devs[0]["usb"])
        self.assertEqual(mic.pick(devs)["alsa"], ":1,0")

    def test_onboard_only_and_empty(self):
        self.assertEqual(mic.parse_arecord(ONBOARD_ONLY), [])
        self.assertEqual(mic.parse_arecord(""), [])
        self.assertIsNone(mic.pick([]))

    def test_no_usb_falls_back_to_first_capture_device(self):
        devs = mic.parse_arecord(HAT_ONLY)
        self.assertFalse(devs[0]["usb"])
        self.assertEqual(mic.pick(devs)["alsa"], ":2,0")

    def test_usb_wins_over_an_earlier_card(self):
        self.assertEqual(mic.pick(mic.parse_arecord(HAT_AND_USB))["alsa"], ":3,0")

    def test_usbid_file_marks_usb(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/card2")
            Path(f"{d}/card2/usbid").write_text("1234:5678\n")
            self.assertTrue(mic.parse_arecord(HAT_ONLY, d)[0]["usb"])

    def test_list_devices_without_arecord(self):
        self.assertIsNone(mic.list_devices(lambda a, t: (127, "")))
        self.assertEqual(mic.list_devices(lambda a, t: (1, "")), [])


class Yaml(unittest.TestCase):
    def test_reads_and_sets_first_source_only(self):
        self.assertEqual(mic.read_device(CONFIG), "sysdefault")
        new = mic.set_device(CONFIG, ":1,0")
        self.assertEqual(mic.read_device(new), ":1,0")
        self.assertIn('              device: ":1,0"  # set by the installer\n', new)
        diff = [(a, b) for a, b in zip(CONFIG.splitlines(), new.splitlines()) if a != b]
        self.assertEqual(len(diff), 1)   # every other line, the second source and rtsp.device included, unchanged

    def test_device_on_the_dash_line(self):
        text = CONFIG.replace("            - name: Default\n              device: sysdefault  # set by the installer\n",
                              "            - device: sysdefault\n              name: Default\n")
        self.assertEqual(mic.read_device(mic.set_device(text, ":1,0")), ":1,0")

    def test_legacy_single_source(self):
        self.assertEqual(mic.read_device(LEGACY), "sysdefault")
        self.assertEqual(mic.set_device(LEGACY, ":1,0"), LEGACY.replace('"sysdefault"', '":1,0"'))

    def test_no_audio_section(self):
        self.assertIsNone(mic.read_device("main:\n    name: x\n"))
        with self.assertRaises(ValueError):
            mic.set_device("main:\n    name: x\n", ":1,0")


class Rule(unittest.TestCase):
    def test_leave_the_owners_device_alone(self):
        self.assertTrue(mic.may_change("sysdefault", None))
        self.assertTrue(mic.may_change("", None))
        self.assertFalse(mic.may_change(":2,0", None))
        self.assertFalse(mic.may_change("hw:CARD=Mic,DEV=0", ":1,0"))
        self.assertTrue(mic.may_change(":1,0", ":1,0"))    # ours from last time: may follow a new card number
        self.assertFalse(mic.may_change(None, None))

    def test_device_ok(self):
        usb = mic.parse_arecord(PI4_USB)
        self.assertFalse(mic.device_ok("sysdefault", usb))   # card 0 cannot record on a Pi 4
        self.assertTrue(mic.device_ok(":1,0", usb))
        self.assertTrue(mic.device_ok("hw:1,0", usb))
        self.assertTrue(mic.device_ok("sysdefault:CARD=Device", usb))
        self.assertFalse(mic.device_ok(":2,0", usb))
        self.assertFalse(mic.device_ok(":1,0", []))


class Apply(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = f"{self.d}/config.yaml"
        self.state = f"{self.d}/state/mic-device"
        Path(self.cfg).write_text(CONFIG)
        os.chmod(self.cfg, 0o640)

    def test_writes_device_backup_and_state(self):
        changed, msg = mic.apply(self.cfg, mic.parse_arecord(PI4_USB), self.state)
        self.assertTrue(changed)
        self.assertIn(":1,0 (USB Lavalier Microphone, USB)", msg)
        self.assertEqual(mic.read_device(Path(self.cfg).read_text()), ":1,0")
        self.assertEqual(Path(self.cfg + mic.BACKUP_SUFFIX).read_text(), CONFIG)
        self.assertEqual(Path(self.state).read_text().strip(), ":1,0")
        self.assertEqual(os.stat(self.cfg).st_mode & 0o777, 0o640)
        # run again: nothing to do
        changed, msg = mic.apply(self.cfg, mic.parse_arecord(PI4_USB), self.state)
        self.assertFalse(changed)
        # the microphone came back as card 2 after a reboot: ours, so it is followed
        changed, _ = mic.apply(self.cfg, mic.parse_arecord(PI4_USB.replace("card 1:", "card 2:")), self.state)
        self.assertTrue(changed)
        self.assertEqual(mic.read_device(Path(self.cfg).read_text()), ":2,0")

    def test_owners_choice_is_kept(self):
        Path(self.cfg).write_text(mic.set_device(CONFIG, "hw:CARD=Mic,DEV=0"))
        before = Path(self.cfg).read_text()
        changed, msg = mic.apply(self.cfg, mic.parse_arecord(PI4_USB), self.state)
        self.assertFalse(changed)
        self.assertIn("left as you set it", msg)
        self.assertEqual(Path(self.cfg).read_text(), before)
        self.assertFalse(os.path.exists(self.cfg + mic.BACKUP_SUFFIX))

    def test_no_microphone_warns_and_leaves_config(self):
        changed, msg = mic.apply(self.cfg, mic.parse_arecord(ONBOARD_ONLY), self.state)
        self.assertFalse(changed)
        self.assertTrue(msg.startswith("WARNING: No microphone found"))
        self.assertEqual(Path(self.cfg).read_text(), CONFIG)

    def test_cli(self):
        rec = f"{self.d}/arecord.txt"
        Path(rec).write_text(PI4_USB)
        out = subprocess.run([sys.executable, str(ROOT / "base" / "mic.py"), "--config", self.cfg, "--state",
                              self.state, "--arecord-output", rec], capture_output=True, text=True, check=True)
        self.assertIn("BirdNET-Go microphone set to :1,0", out.stdout)
        out = subprocess.run([sys.executable, str(ROOT / "base" / "mic.py"), "--list", "--arecord-output", rec],
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout, "Capture device: :1,0 (USB Lavalier Microphone, USB)\n")


class BirdnetGoCheck(unittest.TestCase):
    """base/birdnet-go-check.sh, with a fake docker on PATH."""

    def run_check(self, container: bool, config: bool):
        d = tempfile.mkdtemp()
        bin_dir = f"{d}/bin"
        os.makedirs(bin_dir)
        Path(f"{bin_dir}/docker").write_text("#!/bin/sh\n" + ("exit 0\n" if container else "exit 1\n"))
        os.chmod(f"{bin_dir}/docker", 0o755)
        cfg = f"{d}/config.yaml"
        if config:
            Path(cfg).write_text(CONFIG)
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"])
        return subprocess.run(["bash", str(ROOT / "base" / "birdnet-go-check.sh"), cfg, "0"],
                              capture_output=True, text=True, env=env)

    def test_complete(self):
        r = self.run_check(True, True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_installer_stopped_early(self):
        r = self.run_check(False, False)   # what the first real install left: only the logs folder
        self.assertEqual(r.returncode, 1)
        self.assertIn("config file", r.stdout)
        self.assertIn("Docker container birdnet-go", r.stdout)

    def test_container_missing(self):
        r = self.run_check(False, True)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("config file", r.stdout)


class WnbaseMicrophone(unittest.TestCase):
    def make_app(self, arecord: tuple[int, str], logs: tuple[int, str], device="sysdefault"):
        d = tempfile.mkdtemp()
        os.makedirs(f"{d}/data")
        os.makedirs(f"{d}/config")
        Path(f"{d}/config/config.yaml").write_text(CONFIG.replace("device: sysdefault  #", f'device: "{device}"  #'))
        ini = f"{d}/wdx-agent.ini"
        Path(ini).write_text(f"[agent]\nendpoint = http://localhost:9/x\napi_key =\nsource = birdnet-go\n"
                             f"path = {d}/data/birdnet.db\nstate_file = {d}/state.json\n")

        def run(args, timeout=30):
            if args[:1] == ["arecord"]:
                return arecord
            if args[:2] == ["sh", "-c"] and "docker logs" in args[2]:
                return logs
            return 127, ""
        cfg = wnbase.Config(None, agent_config=ini, agent_module=AGENT, setup_code_file=f"{d}/code")
        return wnbase.App(cfg, runner=run)

    def test_birdnet_go_says_no_audio_source(self):
        logs = (0, "capture started\n...\naudio card capture failed\nstarting without active audio sources\n")
        m = self.make_app((0, PI4_USB), logs).info()["microphone"]
        self.assertIs(m["found"], False)
        self.assertIn("without an active audio source", m["problem"])
        self.assertEqual(m["device"], "sysdefault")

    def test_capture_started(self):
        logs = (0, "starting without active audio sources\nrestart\ncapture started on USB Lavalier Microphone\n")
        m = self.make_app((0, PI4_USB), logs, ":1,0").info()["microphone"]
        self.assertIs(m["found"], True)
        self.assertEqual(m["devices"], [":1,0 (USB Lavalier Microphone, USB)"])

    def test_no_capture_device(self):
        m = self.make_app((0, ONBOARD_ONLY), (127, "")).microphone()
        self.assertIs(m["found"], False)

    def test_configured_device_cannot_record(self):
        m = self.make_app((0, PI4_USB), (127, "")).microphone()   # still sysdefault (card 0)
        self.assertIs(m["found"], False)
        self.assertIn('"sysdefault"', m["problem"])

    def test_unknown_without_tools(self):
        self.assertIsNone(self.make_app((127, ""), (127, "")).microphone()["found"])

    def test_dashboard_has_the_message(self):
        self.assertIn("No microphone found. ", wnbase.DASHBOARD)


if __name__ == "__main__":
    unittest.main()
