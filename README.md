<img src="assets/logo.svg" width="64" alt="WildNetwork">

# WildNetwork Base

**A WildNetwork Base is a small open-hardware station that listens for birds and other wildlife, identifies them on the spot, and adds what it hears to the live [WildNetwork](https://wildnetwork.arunrajiah.com) map.** It is a Raspberry Pi with a microphone (and optionally a 4G modem and a solar battery) in a weatherproof box. It works without internet: it keeps its detections until it can send them, or until a phone carries them out.

**Who it is for:** anyone who wants to put up a listening station, from a garden to a nature reserve, and rangers and researchers who need stations in places without Wi-Fi.

**Get one running in three steps:**

1. **Flash** the WildNetwork Base image onto a microSD card ([how](#flash-an-sd-card)).
2. **Power on** the Base with the card inside and wait about 2 minutes.
3. **Set up** from your phone or any browser: join the Wi-Fi `WildNetwork-XXXX` with the password on the label, open http://10.42.0.1, and enter the station's name and location ([details](#first-time-setup)).

What runs on it: **BirdNET-Go** identifies species from the microphone, **wdx-agent** sends the detections to WildNetwork over 4G, Wi-Fi or Ethernet, and **wnbase** (this repository) serves the dashboard and setup page over the Base's own Wi-Fi.

Phase 0 uses off-the-shelf parts: a Raspberry Pi 4 or 5, a 32 GB or larger microSD card, a USB microphone, and optionally a USB 4G modem and a battery. [Build a Base: parts and step-by-step guide](docs/HARDWARE.md).

> Part of an open wildlife toolkit. Not sure this is the project you need? See [which project to use](#part-of-an-open-wildlife-toolkit).

## Flash an SD card

1. Download the newest `wildnetwork-base` `.img.xz` from [Releases](https://github.com/arunrajiah/wildnetwork-base/releases).
2. Open Raspberry Pi Imager, choose your Raspberry Pi model, then **Operating System > Use custom** and pick the downloaded file. Skip Imager's OS customisation: this image does not use it.
3. Optional, for SSH: after flashing, open the card's boot partition (called `bootfs`) on your computer and add a file named `wildnetwork-ssh.pub` with your SSH public key(s), one per line.
4. Put the card in the Base and power it on. The first boot takes about 2 minutes (it grows the file system to fill the card and loads BirdNET-Go).
5. Join the Wi-Fi `WildNetwork-XXXX` with the password on the label, open http://10.42.0.1 and continue with [First-time setup](#first-time-setup).

Everything the Base needs is on the card, including BirdNET-Go (release 20260823, see `image/VERSIONS`), so the first boot needs no internet. BirdNET-Go picks the first microphone it finds; its own settings page is at http://10.42.0.1:8080.

**No label?** The hotspot name, password and setup code are made on the Base at its first boot. They are shown on the login screen of a monitor plugged into the HDMI port, and written to `wildnetwork-setup.txt` on the boot partition (put the card in a computer to read it). Anyone holding the SD card can read that file; that is acceptable, because anyone with the card in hand already has full access to the Base.

**Accounts and SSH.** The image has one user, `wildnetwork`. Its password is a long random one made at first boot and never shown, so there is no default password, and the root account is locked. SSH is off. It switches on, key only (no passwords, no root login), when `wildnetwork-ssh.pub` is on the boot partition; the file is read at every boot, so removing it switches SSH off again at the next boot. Log in with `ssh wildnetwork@wildnetwork-base-xxxx.local` (or `@10.42.0.1` over the hotspot); `sudo` works without a password for this user, because there is no password to type.

**Wi-Fi country.** The image is set to `GB` so the hotspot can start offline (2.4 GHz is allowed on the same channels almost everywhere). Change it with `sudo raspi-config nonint do_wifi_country XX` if you use the Base's Wi-Fi to join a network in another country.

## First-time setup

Each Base has its own hotspot and setup code. The label on the device shows:

- **Wi-Fi:** `WildNetwork-XXXX` (the last 4 characters of the Raspberry Pi serial number)
- **Password:** `wn-` followed by the setup code
- **Setup code:** 8 letters and digits

**With the WildNetwork field app:** join the hotspot, open the app and follow the steps. The app reads `/api/info` and sends your location, key and connection details to `/api/config`.

**With a browser:** join the hotspot, open http://10.42.0.1 and open **Setup**. Enter:

1. the setup code from the label;
2. a name for the station and its location (type the coordinates, or tap **Use this phone's location**; some phone browsers only share the location on secure pages, so typing always works);
3. how the Base gets internet: the APN of its SIM card for 4G, or a Wi-Fi network near it. Leave both empty if a phone will carry the data out.

That is all. The Base then connects to WildNetwork by itself: as soon as it has internet it registers as a new device and gets its own key, so nobody has to handle a key. The dashboard shows "Connected to WildNetwork", or "Will connect to WildNetwork when the Base has internet" until then. If you registered the Base yourself, put its key and device id under **Advanced**; a key that is already there is always kept.

The hotspot runs whenever the Wi-Fi radio is not connected to a network. When the Base joins a Wi-Fi network (the one set during setup, or one set when the SD card was flashed) the hotspot stops, and the dashboard is at `http://<hostname>.local` on that network. To keep the hotspot on regardless, set `force = yes` under `[hotspot]` in `/etc/wnbase/wnbase.ini`.

## Data pickup (Base without a connection)

When the Base has no 4G or Wi-Fi, a phone carries the data out:

1. The phone joins the hotspot and calls `GET /api/detections`. It gets a batch of WDX events and a `cursor`.
2. The phone uploads the events to WildNetwork, `POST /api/v1/events`, with its own key.
3. The phone calls `POST /api/ack` with that `cursor` and the setup code. The Base moves its queue past the batch.
4. Repeat until `events` is empty.

If a step fails, nothing is lost: the same batch is offered again. WildNetwork deduplicates by `eventId`, so sending a batch twice is harmless. If the Base's own uploader has already moved past the batch, the ack is accepted but the queue is not moved backwards.

## Security notes

- The hotspot password and the setup code are different on every Base and are made on the device the first time it starts (`/etc/wnbase/setup-code`, readable by root only).
- Changing settings and acknowledging a pickup need the setup code. Wrong codes are rate limited per address.
- The WildNetwork API key never leaves the Base through this API: it is written to `/etc/wdx-agent.ini` and is not returned by any endpoint or written to the log.
- Coordinates in uploaded events are rounded on the device (`roundCoords`, default 2 decimals, about 1 km).
- Reading endpoints need no code: anyone who knows the hotspot password can see the dashboard. Keep the label out of sight on public sites.

## Install on a stock Raspberry Pi

1. Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. Set a user, and set your Wi-Fi country.
2. Copy this repository to the Pi (or `git clone` it) and run:

```sh
sudo ./install.sh
```

The installer:

- installs `network-manager`, `modemmanager`, `sqlite3`, `python3` and `curl`;
- installs wdx-agent from `https://wildnetwork.arunrajiah.com/agent/wdx_agent.py` (or a `wdx_agent.py` placed next to `install.sh` when offline) into `/opt/wdx-agent`;
- installs BirdNET-Go with its official installer (`https://github.com/tphakala/birdnet-go/raw/main/install.sh`, Docker based, data in `~/birdnet-go-app/data/birdnet.db` and clips in `~/birdnet-go-app/data/clips`) unless it is already there;
- writes `/etc/wdx-agent.ini` (source `birdnet-go`) and `/etc/wnbase/wnbase.ini` if they do not exist;
- installs and enables `wnbase.service`, `wn-hotspot.service` (plus `wn-hotspot.timer`) and `wdx-agent.service`;
- prints the hotspot name, its password and the setup code.

It is safe to run again: configs, the setup code and BirdNET-Go are kept. Options are environment variables: `WN_USER`, `WN_BASE_URL`, `WN_SKIP_BIRDNET=1`, `WN_BIRDNET_VERSION` (default `latest`), `WN_WIFI_COUNTRY` (two-letter code, needed once for the hotspot if it was not set when flashing).

## For developers

### Build the SD card image

```sh
./image/build.sh
```

This uses the official [pi-gen](https://github.com/RPi-Distro/pi-gen) at the commit pinned in `image/VERSIONS` (Raspberry Pi OS Lite, trixie, 64-bit: stages 0 to 2 plus `image/stage-wildnetwork`). It needs Docker with privileged containers and about 15 GB of free disk; `WN_PRUNE_WORK=1` saves about 3 GB. The result is `image/deploy/*.img.xz`. The `SD card image` GitHub Actions workflow runs the same script when started by hand or when a `v*` tag is pushed, and attaches the image to that tag's release.

The image stage runs `base/provision.sh --image`, the same steps as `install.sh` without starting anything, then adds the BirdNET-Go arm64 container (downloaded with crane, checked against the pinned digest) for `wn-firstboot.service` to load.

### Local API

All responses are JSON. GET endpoints allow any origin (CORS). POST endpoints need the header `X-Setup-Code`.

#### GET /api/info

No code needed.

```json
{"hardwareId": "10000000abcd1234", "model": "wildnetwork-base", "hostname": "wnbase",
 "software": {"wnbase": "0.1.0", "wdx-agent": "0.3.0", "birdnetGo": "latest"},
 "configured": true, "deviceId": "wnb_1", "name": "Hill top", "latitude": 51.5, "longitude": -0.12,
 "registration": "registered"}
```

`configured` is true once the location is set. `registration` is `registered`, `waiting-for-setup` (no name or location yet), `waiting-for-internet`, or `error: <reason>` (for example too many new devices from one network in a day; it retries every hour). The key itself is never returned.

**Registration.** While the agent config has a name and location but no `api_key`, wnbase calls `POST <endpoint base>/api/v1/devices` with `{"name", "model": "wildnetwork-base", "hardwareId"}` every 5 minutes (backing off to 1 hour after failures, and waiting an hour after a `429`), writes the returned `api_key`, `device_id` and `system = wildnetwork-base` to `/etc/wdx-agent.ini`, and restarts wdx-agent. Saving the setup triggers an attempt straight away. `birdnetGo` is the BirdNET-Go image tag, or null.

#### POST /api/config

Header `X-Setup-Code`. Every field is optional; only the fields sent are changed.

```json
{"name": "Hill top", "latitude": 51.5, "longitude": -0.12, "roundCoords": 2,
 "apiKey": "wn_...", "deviceId": "wnb_1", "endpoint": "https://wildnetwork.arunrajiah.com/api/v1/events",
 "apn": "internet", "wifi": {"ssid": "Farm office", "psk": "secret123"}}
```

Writes `/etc/wdx-agent.ini` (other keys are kept) and restarts wdx-agent. A new `latitude`/`longitude` is also written, unrounded, into BirdNET-Go's `config.yaml` (only the `latitude` and `longitude` keys directly under `birdnet:` change; the rest of the file is kept as it was), and the `birdnet-go` container is restarted, so BirdNET-Go's species range filter uses the real location. At 0,0 it would accept species that do not occur there. The exact location stays on the device; `roundCoords` only applies to what wdx-agent uploads. `apn` creates or updates the NetworkManager connection `wn-4g`; `wifi` saves the connection `wn-uplink`, which is used the next time the radio is free. Response:

```json
{"ok": true, "configured": true}
```

A `warnings` list is added when, for example, no modem is present, and a `notes` list when BirdNET-Go could not be updated (for example `config.yaml` not found: then set the location in BirdNET-Go's web page on port 8080). Wrong code: `403`. After 10 wrong codes in 10 minutes from one address: `429`. Bad values: `400`.

#### GET /api/status

Device health in the WDX `device-status` format from wdx-agent, plus `lastUpload` (when the queue last moved, by upload or pickup) and `network`:

```json
{"wdx": "0.2", "kind": "device-status", "deviceId": "wnb_1", "at": "2026-10-08T10:00:00Z",
 "software": {"wdx-agent": "0.3.0"}, "storage": {"freeMb": 51200, "totalMb": 62000},
 "temperatureC": 48.2, "uptimeSeconds": 86400, "battery": {"percent": 81, "volts": 13.1},
 "queue": {"pending": 12}, "lastUpload": "2026-10-08T09:58:10Z",
 "network": {"active": [{"name": "wn-4g", "type": "gsm", "device": "cdc-wdm0"}], "uplink": "4g", "hotspot": true,
             "cellular": {"signalPercent": 70, "accessTechnology": "lte", "operator": "EE", "state": "connected"}}}
```

`queue.pending` counts one batch at most, so 500 means 500 or more.

#### GET /api/detections?limit=N

Up to N (default and maximum 500) WDX events not yet uploaded, and the cursor to acknowledge them with:

```json
{"cursor": {"d": 4}, "events": [{"wdx": "0.1", "eventId": "birdnet-go:station1:d4", "...": "..."}]}
```

`cursor` is null and `events` empty when nothing is waiting. The cursor can also cover skipped rows (low confidence, noise), so a batch can have a cursor and no events: acknowledge it anyway.

#### POST /api/ack

Header `X-Setup-Code`. Body `{"cursor": <cursor from the last /api/detections response>}`. Response `{"ok": true, "advanced": true}`. Any other cursor is refused with `409`.

#### GET /api/recent?hours=24

Detections from the BirdNET-Go database for the dashboard, newest first, at most 200 (hours 1 to 168):

```json
{"hours": 24, "detections": [{"id": 4, "time": "2026-10-08T09:41:00Z", "scientificName": "Turdus merula",
  "commonName": null, "confidence": 0.92, "clip": "turdus_merula_92p_20261008T094100Z.wav", "unlikely": false}]}
```

#### GET /clips/&lt;name&gt;

The audio clip with that file name from the BirdNET-Go clips folder. Only plain file names are accepted; anything with a path is refused. Supports `Range` requests.

#### GET /

The dashboard: health, the last 24 hours of detections with play buttons, and the setup form. One page with no outside resources, so it works without internet.

### Files

| Path | What |
| --- | --- |
| `base/wnbase.py` | Dashboard and local API (`/opt/wnbase/wnbase.py`) |
| `base/hotspot.sh` | Creates and keeps the hotspot (`/opt/wnbase/hotspot.sh`) |
| `install.sh` | Installer for a stock Raspberry Pi |
| `base/provision.sh` | Steps shared by `install.sh` and the image build |
| `base/firstboot.sh`, `base/ssh-key.sh`, `base/birdnet-go-unit.sh`, `base/audio-detect.sh` | Image only: first boot, SSH from the boot partition, BirdNET-Go unit, microphone choice |
| `image/` | SD card image: `build.sh`, `VERSIONS`, pi-gen stage |
| `systemd/` | `wnbase.service`, `wn-hotspot.service`, `wn-hotspot.timer`, `wdx-agent.service`, `wn-firstboot.service`, `wn-ssh-key.service` |
| `tests/` | `python3 -m unittest discover -s tests` (needs wdx-agent checked out next to this repo, or `WDX_AGENT_PY`) |

`/etc/wnbase/wnbase.ini`:

```ini
[wnbase]
port = 80
agent_config = /etc/wdx-agent.ini
agent_module = /opt/wdx-agent/wdx_agent.py
agent_service = wdx-agent
# empty: the path in /etc/wdx-agent.ini
birdnet_db =
clips_dir = /home/pi/birdnet-go-app/data/clips
# empty: ../config/config.yaml from the database folder
birdnet_config = /home/pi/birdnet-go-app/config/config.yaml
birdnet_container = birdnet-go
setup_code_file = /etc/wnbase/setup-code

[hotspot]
ifname = wlan0
ssid_prefix = WildNetwork-
force = no
```

## Part of an open wildlife toolkit

Eight open source projects for listening to, identifying and mapping wildlife. They work together, but you rarely need more than one or two. Start from what you want to do:

| I want to | Use | What it is |
|---|---|---|
| See where birds and wildlife are moving, or download the data | [WildNetwork](https://github.com/arunrajiah/wildnetwork) | The live map and open data: [wildnetwork.arunrajiah.com](https://wildnetwork.arunrajiah.com) |
| Build a monitoring station from open hardware | [WildNetwork Base](https://github.com/arunrajiah/wildnetwork-base) (this project) | Software for the open WildNetwork station (Raspberry Pi, microphone, solar); SD card image coming |
| Share detections from a BirdNET-Pi, BirdNET-Go, camera trap or bat detector you already have | [wdx-agent](https://github.com/arunrajiah/wdx-agent) | One small program that sends your station's detections. BirdWeather stations are already included and need nothing |
| Follow your own station on your phone | [BirdEcho](https://github.com/arunrajiah/birdecho) | Android app for BirdNET-Pi, BirdNET-Go and BirdWeather stations |
| Identify a sound you just heard | [WildEcho](https://github.com/arunrajiah/wildecho) | Phone app: record a clip, get ranked species |
| Run your own sound identification server | [wildecho-api](https://github.com/arunrajiah/wildecho-api) | Self-hosted species identification from audio, on CPU, no API keys |
| Check camera trap predictions before you use them | [SpeciesNet Studio](https://github.com/arunrajiah/speciesnet-studio) | Self-hosted review of SpeciesNet results |
| Make your own software or device produce or read detections in a common format | [WDX](https://github.com/arunrajiah/wildlife-detection-exchange) | The open format for one AI wildlife detection; maps to Darwin Core |

**How this one fits.** The WildNetwork Base is the open station. It identifies birds on the device with BirdNET-Go, sends detections to WildNetwork through wdx-agent, and is set up from a phone or a browser.

**How they connect:** stations (a WildNetwork Base, BirdNET-Pi, BirdNET-Go, camera traps) produce detections; wdx-agent sends them in the WDX format; WildNetwork maps them. Connected today: wdx-agent and the WildNetwork Base send to WildNetwork, and WildEcho uses wildecho-api. Planned: Base setup in BirdEcho, and WDX export from wildecho-api and SpeciesNet Studio.

## Licence

Apache-2.0. See `LICENSE`.

## Name and logo

The code is open source (Apache-2.0). The name "WildNetwork" and the logo are not part of the code licence: forks that run as a public service or ship devices should use their own name and logo. See [TRADEMARKS.md](TRADEMARKS.md).
