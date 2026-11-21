<img src="assets/logo.svg" width="64" alt="Wildlife Network">

# Wildlife Network Base

**A Wildlife Network Base is a small open-hardware station that listens for birds and other wildlife, identifies them on the spot, and adds what it hears to the live [Wildlife Network](https://wlnw.org) map.** It is a Raspberry Pi with a microphone (and optionally a 4G modem and a solar battery) in a weatherproof box. It works without internet: it keeps its detections until it can send them, or until a phone carries them out.

**Who it is for:** anyone who wants to put up a listening station, from a garden to a nature reserve, and rangers and researchers who need stations in places without Wi-Fi.

**Get one running in three steps:**

1. **Flash** the Wildlife Network Base image onto a microSD card ([how](#flash-an-sd-card)).
2. **Power on** the Base with the card inside and wait about 2 minutes.
3. **Set up** from your phone or any browser: join the Wi-Fi `Wildlife Network-XXXX` with the password on the label, open http://10.42.0.1, and enter the station's name and location ([details](#first-time-setup)).

What runs on it: **BirdNET-Go** identifies species from the microphone, **wdx-agent** sends the detections to Wildlife Network over 4G, Wi-Fi or Ethernet, and **wnbase** (this repository) serves the dashboard and setup page over the Base's own Wi-Fi.

Phase 0 uses off-the-shelf parts: a Raspberry Pi 4 or 5, a 32 GB or larger microSD card, a USB microphone, and optionally a USB 4G modem and a battery. [Build a Base: parts and step-by-step guide](docs/HARDWARE.md).

> Part of an open wildlife toolkit. Not sure this is the project you need? See [which project to use](#part-of-an-open-wildlife-toolkit).

## Flash an SD card

1. Download the newest `wildnetwork-base` `.img.xz` from [Releases](https://github.com/arunrajiah/wildnetwork-base/releases), and the `.img.xz.sha256` file next to it.
   Check that the download is complete and unchanged: in the folder with both files run `sha256sum -c *.img.xz.sha256` (Linux), `shasum -a 256 -c *.img.xz.sha256` (macOS) or, in PowerShell, `Get-FileHash .\<file>.img.xz` and compare the result with the text in the `.sha256` file (Windows). It must say `OK` or match exactly; otherwise download again.
2. Open Raspberry Pi Imager, choose your Raspberry Pi model, then **Operating System > Use custom** and pick the downloaded file. Skip Imager's OS customisation: this image does not use it.
3. Optional, for SSH: after flashing, open the card's boot partition (called `bootfs`) on your computer and add a file named `wildnetwork-ssh.pub` with your SSH public key(s), one per line.
4. Put the card in the Base and power it on. The first boot takes about 2 minutes (it grows the file system to fill the card and loads BirdNET-Go).
5. Join the Wi-Fi `Wildlife Network-XXXX` with the password on the label, open http://10.42.0.1 and continue with [First-time setup](#first-time-setup).

Everything the Base needs is on the card, including BirdNET-Go (release 20260823, see `image/VERSIONS`), so the first boot needs no internet. The Base points BirdNET-Go at the first USB microphone it finds (see [The microphone](#the-microphone)); BirdNET-Go's own settings page is at http://10.42.0.1:8080.

**No label?** The hotspot name, password and setup code are made on the Base at its first boot. They are shown on the login screen of a monitor plugged into the HDMI port, and written to `wildnetwork-setup.txt` on the boot partition (put the card in a computer to read it). Anyone holding the SD card can read that file; that is acceptable, because anyone with the card in hand already has full access to the Base.

**Accounts and SSH.** The image has one user, `wildnetwork`. Its password is a long random one made at first boot and never shown, so there is no default password, and the root account is locked. SSH is off. It switches on, key only (no passwords, no root login), when `wildnetwork-ssh.pub` is on the boot partition; the file is read at every boot, so removing it switches SSH off again at the next boot. Log in with `ssh wildnetwork@wildnetwork-base-xxxx.local` (or `@10.42.0.1` over the hotspot); `sudo` works without a password for this user, because there is no password to type.

**Wi-Fi country.** The image is set to `GB` so the hotspot can start offline (2.4 GHz is allowed on the same channels almost everywhere). Change it with `sudo raspi-config nonint do_wifi_country XX` if you use the Base's Wi-Fi to join a network in another country.

## First-time setup

Each Base has its own hotspot and setup code. The label on the device shows:

- **Wi-Fi:** `Wildlife Network-XXXX` (the last 4 characters of the Raspberry Pi serial number)
- **Password:** `wn-` followed by the setup code
- **Setup code:** 8 letters and digits

**With the Wildlife Network field app:** join the hotspot, open the app and follow the steps. The app reads `/api/info` and sends your location, key and connection details to `/api/config`.

**With a browser:** join the hotspot, open http://10.42.0.1 and open **Setup**. Enter:

1. the setup code from the label;
2. a name for the station and its location (type the coordinates, or tap **Use this phone's location**; some phone browsers only share the location on secure pages, so typing always works);
3. how the Base gets internet: the APN of its SIM card for 4G, or a Wi-Fi network near it. Leave both empty if a phone will carry the data out;
4. optional: tick **Test station** while you try the Base out (on a table, at home). Its detections are then kept apart and not shown on the public map, and the dashboard says "Test station, not on the public map". Untick it once the Base is in its real place.

**Time zone.** Saving the location also sets the Base's time zone, so BirdNET-Go's own page, the BirdEcho Feed and Studio show detection times in local time (a new card starts on UTC). The zone is looked up on the Base itself, without internet, from a map of time zones bundled with it (`base/tzgrid.dat`, about 5 km cells; within a few km of a border between zones it can pick the neighbouring zone). The Base then sets the system zone (`timedatectl set-timezone`), changes `TZ` in BirdNET-Go's service and restarts BirdNET-Go and wdx-agent; the log says so (`journalctl -u wnbase`). The dashboard footer shows the zone in use. To choose the zone yourself, type it under **Advanced** in Setup (an IANA name such as `Asia/Kolkata`; empty means from the location), send `timezone` to `POST /api/config`, set `timezone =` under `[base]` in `/etc/wnbase/wnbase.ini`, or put `wildnetwork-timezone.txt` on the card (below). A zone set this way always wins over the looked-up one. A Base set up before version 0.2.2 switches by itself at its next start. Uploads are not affected: wdx-agent always sends true UTC times to Wildlife Network.

That is all. The Base then connects to Wildlife Network by itself: as soon as it has internet it registers as a new device and gets its own key, so nobody has to handle a key. The dashboard shows "Connected to Wildlife Network", or "Will connect to Wildlife Network when the Base has internet" until then. If you registered the Base yourself, put its key and device id under **Advanced**; a key that is already there is always kept.

**Updates.** The Base keeps itself up to date: once a night, and 10 minutes after it starts, it installs new versions of its own software and of wdx-agent when it has internet (see [Automatic updates](#automatic-updates)). To stop that, untick **Keep the Base up to date automatically** under **Advanced** in Setup. The line under it says when it last checked and what happened.

The hotspot is a fallback: home Wi-Fi comes first. `wn-net.service` (`base/wnnet.py`) decides, and logs every switch and its reason with the tag `wn-net` (`journalctl -t wn-net`):

- **At boot** home Wi-Fi gets up to 90 seconds: the Base scans and tries its saved Wi-Fi network. Only if it is still not connected does the hotspot start. With no Wi-Fi network saved at all, the hotspot starts at once.
- **In hotspot mode** the Base tries home Wi-Fi again every 5 minutes, but only while no phone is connected to the hotspot, so a setup in progress is never interrupted. When home Wi-Fi works, the hotspot goes down.
- **When home Wi-Fi drops** (a router restarting, a short outage) the Base waits 3 minutes before starting the hotspot.

`tests/test_wnnet.py` checks this against a simulated NetworkManager: boot with home Wi-Fi in range, five power cuts in a row, boot without it and the router coming back later, a router restart in normal running, and a phone on the hotspot during setup. On a real Base, do the power-cut check in [docs/HARDWARE.md](docs/HARDWARE.md#3-test-it-on-a-table-first).

The hotspot profile `wn-hotspot` never connects by itself (`autoconnect no`, also fixed on Bases set up earlier, at every start of `wn-net`): an access point needs no scan, so it used to take the radio at boot before home Wi-Fi had finished scanning, and the Base stayed in hotspot mode after a power cut. The times are `boot_wait_seconds`, `retry_seconds` and `fallback_seconds` under `[hotspot]` in `/etc/wnbase/wnbase.ini`; restart with `sudo systemctl restart wn-net`. To keep the hotspot on regardless, set `force = yes` there. On home Wi-Fi the dashboard is at `http://<hostname>.local`.

### Set home Wi-Fi from the SD card

If the Base cannot reach its Wi-Fi any more (new router, new password) and you cannot join its hotspot, put the SD card in a computer and create `wildnetwork-wifi.txt` on the boot partition (`bootfs`) with a plain text editor:

```text
ssid=My Home Wi-Fi
password=the Wi-Fi password
country=IN
```

`country` (two letters) is optional. At the next start the Base saves this as its home Wi-Fi (the `wn-uplink` profile, other Wi-Fi profiles switched off) and then deletes the file, so the password does not stay on the card. It leaves `wildnetwork-wifi.txt.applied` with the network name and the result, never the password. A password must have 8 to 63 characters; a file without a usable network name or password is not applied, but is still removed.

### Set the time zone from the SD card

To give the Base a time zone other than the one of its location, create `wildnetwork-timezone.txt` on the boot partition with one line, for example:

```text
timezone=Asia/Kolkata
```

At the next start the Base stores it (as `[base] timezone` in `/etc/wnbase/wnbase.ini`), switches to it and renames the file to `wildnetwork-timezone.txt.applied` with the result, so a zone chosen later in Setup is not overwritten. `timezone=auto` goes back to the zone of the location. A name the Base does not know is not applied.

## Data pickup (Base without a connection)

When the Base has no 4G or Wi-Fi, a phone carries the data out:

1. The phone joins the hotspot and calls `GET /api/detections`. It gets a batch of WDX events and a `cursor`.
2. The phone uploads the events to Wildlife Network, `POST /api/v1/events`, with its own key.
3. The phone calls `POST /api/ack` with that `cursor` and the setup code. The Base moves its queue past the batch.
4. Repeat until `events` is empty.

If a step fails, nothing is lost: the same batch is offered again. Wildlife Network deduplicates by `eventId`, so sending a batch twice is harmless. If the Base's own uploader has already moved past the batch, the ack is accepted but the queue is not moved backwards.

## Automatic updates

A Base installs new versions of its own software by itself, so a Base out in the field does not keep running an old version. `wnbase-update.service` (`base/wnupdate.py`) runs once a night at a random time between 01:00 and 05:00 local time (`wnbase-update.timer`, `RandomizedDelaySec=4h`, so not every Base asks at once) and 10 minutes after every start (`wnbase-update-boot.timer`). It does nothing when the switch is off or when the Base has no internet connection of its own (hotspot only, or no network at all).

What it updates:

- **The Base software** (wnbase, the files in `/opt/wnbase` and its systemd units) from the newest release of this repository on GitHub. Each release carries `wnbase-<version>.tar.gz` (the repository at that tag, made by `git archive` in `.github/workflows/image.yml`) and `wnbase-<version>.tar.gz.sha256`. The Base downloads both over HTTPS, checks the sha256, checks that `base/wnbase.py` inside says the same version as the release tag and that every Python file compiles, and installs it with that release's own `base/provision.sh --update` (files and units only: no packages, no configs, nothing restarted). Pre-releases and drafts are never taken, and a version is never downgraded. Releases before 0.3.0 have no update file: a Base on 0.2.x needs one last manual update (run `install.sh` again, or flash 0.3.0), after which it updates itself.
- **wdx-agent** from `<site>/agent/` (`VERSION`, then `wdx_agent.py`; `<site>` is the Wildlife Network address in `/etc/wdx-agent.ini`). The `VERSION = "..."` inside the downloaded file must match, it must be wdx-agent and it must compile.
- **Not** BirdNET-Go: it stays at the release pinned in `image/VERSIONS`, whose database layout wdx-agent is tested against. **Not** the operating system either.

**Safety.** The previous version stays on disk (`/opt/wnbase.prev`, with copies of the units, and `/opt/wdx-agent/wdx_agent.py.prev`). After installing, the Base restarts the service and waits up to 90 seconds for it to come up with the new version: wnbase must answer `/api/info` with it, and a running wdx-agent must log it at start (`wdx-agent X source=...`, read from that run of the service only); wnbase is restarted after a wdx-agent update too, because it loads wdx-agent's code. If not, the previous version is put back and restarted. `/etc/wdx-agent.ini`, `/etc/wnbase/wnbase.ini`, keys, the setup code, the time zone and Wi-Fi are never touched. `wn-net` is restarted only when `wnnet.py` changed; it leaves a working home Wi-Fi connection as it is.

**Switch.** Setup > Advanced > **Keep the Base up to date automatically** (on by default), `"autoUpdate": true|false` in `POST /api/config`, or `auto_update = yes|no` under `[base]` in `/etc/wnbase/wnbase.ini`. Running it by hand always goes ahead, also when switched off:

```sh
sudo /opt/wnbase/wnupdate.py --now      # check and install now
sudo /opt/wnbase/wnupdate.py --check    # only look up the newest versions
journalctl -t wnbase-update             # every check and update, with the reason for a rollback
```

The last result is kept in `/var/lib/wnbase/update.json` and shown in `/api/info` as `update`.

## Security notes

- The hotspot password and the setup code are different on every Base and are made on the device the first time it starts (`/etc/wnbase/setup-code`, readable by root only).
- They are also written to `wildnetwork-setup.txt` on the boot partition, by the first boot of the image and by every run of `install.sh` on a stock Raspberry Pi, so a Base whose label is lost can still be set up. Anyone who can take the SD card out can read that file (and the whole card), so keep the Base closed and the card inside. Delete the file if you prefer: it is not needed to run the Base, and `install.sh` writes it again when run again. The install log `/var/log/wnbase-install.log` holds the same details and is readable by the Base's user and root.
- Changing settings and acknowledging a pickup need the setup code. Wrong codes are rate limited per address.
- The Wildlife Network API key never leaves the Base through this API: it is written to `/etc/wdx-agent.ini` and is not returned by any endpoint or written to the log.
- Health reports (see [Health reports](#health-reports)) carry no audio, no detections and no location.
- `wildnetwork-wifi.txt` on the boot partition is read once at boot and deleted; the password is not kept on the card.
- Automatic updates come only over HTTPS from this repository's GitHub releases (checked against the sha256 published with the release) and from `<site>/agent/`. The sha256 guards against a broken or cut-off download; it comes from the same release, so it does not protect against someone who controls the GitHub repository. Switch updates off in Setup if you prefer to update by hand.
- Coordinates in uploaded events are rounded on the device (`roundCoords`, default 2 decimals, about 1 km).
- Reading endpoints need no code: anyone who knows the hotspot password can see the dashboard. Keep the label out of sight on public sites.
- **BirdNET-Go's page on port 8080** uses BirdNET-Go's own login (`security.basicauth`), switched on by `base/bngauth.py` at install and first boot with a random password in `/etc/wnbase/birdnet-go-password` (root only). The dashboard shows it after the setup code (**Show BirdNET-Go password** in Setup, or `POST /api/birdnet-go-password` with `X-Setup-Code`). A password you set in BirdNET-Go yourself is kept.

  What stays open on the network (no login), as BirdNET-Go 20260823 decides: its dashboard pages, `GET /api/v2/detections`, `/api/v2/detections/recent`, `/api/v2/detections/<id>` and the other read-only views (species, statistics, clips, spectrograms), so apps that read detections, such as BirdEcho, work as before at `http://<base>:8080`.
  What needs the login: all settings (`/api/v2/settings`), control actions (`/api/v2/control/...`: restart, reload model), and changes to detections (delete, review, lock, ignore species, batch actions).
  The wnbase dashboard and API on port 80 are separate and work as described above.

## Install on a stock Raspberry Pi

1. Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. Set a user, and set your Wi-Fi country.
2. Copy this repository to the Pi (or `git clone` it) and run:

```sh
sudo ./install.sh
```

The installer:

- installs `network-manager`, `modemmanager`, `sqlite3`, `python3` and `curl`;
- installs wdx-agent (0.4.5 or later logs an hourly upload summary, also right after the network comes back, and retries within a minute after a network error; its service starts after `network-online.target`) from `https://wlnw.org/agent/wdx_agent.py` (or a `wdx_agent.py` placed next to `install.sh` when offline) into `/opt/wdx-agent`;
- installs BirdNET-Go with its official installer (the one from the pinned release's tag, Docker based, data in `~/birdnet-go-app/data/birdnet.db` and clips in `~/birdnet-go-app/data/clips`) unless it is already there, then checks that the `birdnet-go` container and `~/birdnet-go-app/config/config.yaml` exist. If either is missing it stops with an error and the command to run again. The official installer calls `sudo` itself: when that would ask for a password, `install.sh` asks for it first, or stops with a clear message when there is no terminal (run it from an SSH session with a terminal);
- sets BirdNET-Go's microphone (see [The microphone](#the-microphone)) and restarts BirdNET-Go when it changed;
- writes `/etc/wdx-agent.ini` (source `birdnet-go`) and `/etc/wnbase/wnbase.ini` if they do not exist;
- installs and enables `wnbase.service`, `wn-net.service` (home Wi-Fi first, hotspot as fallback; it replaces the older `wn-hotspot.service` and `wn-hotspot.timer`, which are removed), `wdx-agent.service` and the [automatic update](#automatic-updates) timers;
- prints the hotspot name, its password and the setup code;
- keeps the system journal on the card across reboots and power cuts (`/etc/systemd/journald.conf.d/wnbase.conf`: `Storage=persistent`, at most 200 MB), so `journalctl -b -1` shows what happened before the last restart;
- writes everything it prints to `/var/log/wnbase-install.log` (appended on every run, readable by the Base's user without sudo) and prints that path at the end, also when it stops with an error.

It is safe to run again: configs, the setup code and BirdNET-Go are kept, and the microphone is checked again. Options are environment variables: `WN_USER`, `WN_BASE_URL`, `WN_SKIP_BIRDNET=1`, `WN_BIRDNET_VERSION` (default: the release pinned in `image/VERSIONS`, the same as the SD card image, currently `20260823`; `latest` takes BirdNET-Go's newest, whose database layout wdx-agent may not know yet), `WN_WIFI_COUNTRY` (two-letter code, needed once for the hotspot if it was not set when flashing), `WN_INSTALL_LOG`.

### One command, or no keyboard at all

**One command** on the Pi (over SSH, as the user made in Imager), instead of copying the repository:

```sh
curl -fsSL https://raw.githubusercontent.com/arunrajiah/wildnetwork-base/main/bootstrap.sh | sudo bash
```

`bootstrap.sh` clones this repository to `/opt/wnbase-src` (branch or tag `WN_REF`, default `main`) and runs `install.sh` from it.

**No keyboard, no SSH:** flash, power on, and the Base installs itself.

1. In Raspberry Pi Imager, choose Raspberry Pi OS Lite (64-bit), and in OS customisation set a user, the Wi-Fi network the Pi will use for the install (it needs internet), the Wi-Fi country and, if you like, SSH with your key.
2. After writing, open the card's boot partition (`bootfs`) on your computer, open the file `user-data` (Imager wrote it; it is cloud-init settings) in a plain text editor and add, at the end:

   ```yaml
   runcmd:
     - [bash, -c, "curl -fsSL https://raw.githubusercontent.com/arunrajiah/wildnetwork-base/main/bootstrap.sh | bash -s -- --service"]
   ```

   If the file already has a `runcmd:` line, add only the line starting with `-` under it, indented like the lines already there (a second `runcmd:` would replace the first).
3. Optional: add a file `wildnetwork-firstboot.env` on the boot partition with settings, one `KEY=value` per line: `WN_WIFI_COUNTRY`, `WN_USER`, `WN_REF`, `WN_BASE_URL`, `WN_BIRDNET_VERSION`, `WN_SKIP_BIRDNET`. Other lines are ignored and nothing in it is run.
4. Put the card in the Pi and power it on. On the first boot cloud-init sets up `wn-install.service`, which waits for the network, runs `install.sh` as root, tries again every 2 minutes if it fails, and disables itself once it has succeeded. That takes about 15 to 30 minutes (BirdNET-Go is a large download).
5. When it is done, `wildnetwork-setup.txt` on the boot partition has the hotspot name, password and setup code, and the dashboard is at `http://<hostname>.local` on the same Wi-Fi. Progress: `/var/log/wnbase-install.log`, or `journalctl -u wn-install` over SSH.

BirdNET-Go's installer calls `sudo` as the Pi's user, which needs sudo without a password (Raspberry Pi OS normally sets that up for the user made in Imager). If it cannot, the service stops with that reason in the log: run `sudo ./install.sh` from an SSH session with a terminal instead.

### The microphone

BirdNET-Go's default audio device is `sysdefault`, which on a Raspberry Pi is the onboard audio (card 0). It cannot record, so BirdNET-Go would start without an audio source. `base/mic.py` fixes that:

- it reads `arecord -l` and picks the first USB capture device (without one, the first capture device of any kind);
- it writes it into `~/birdnet-go-app/config/config.yaml` as `realtime.audio.sources[0].device: ":<card>,<device>"` (for example `":1,0"`), changing only that line, and keeps the previous file as `config.yaml.before-mic`;
- it runs from `install.sh` (every run) and before every BirdNET-Go start (a line in the image's unit, a drop-in `birdnet-go.service.d/wnbase-mic.conf` on a stock Pi), so a microphone plugged in later, or one that gets a new card number, is followed;
- it never changes a device you chose: the setting is only replaced while it says `sysdefault` (or is empty), or still holds the value `mic.py` wrote last (kept in `/var/lib/wnbase/mic-device`);
- with no capture device at all it prints a warning and leaves the config alone.

To see what it finds: `python3 /opt/wnbase/mic.py --list`. To use another microphone, choose it in BirdNET-Go's audio settings (port 8080) or set `device` in `config.yaml` yourself and restart BirdNET-Go (`sudo systemctl restart birdnet-go`); your choice is kept from then on. To go back to automatic choice, set `device: sysdefault`.

The dashboard shows **No microphone found** when BirdNET-Go's log says it started without an active audio source, when `arecord -l` lists no capture device, or when the device in `config.yaml` names a card that cannot record. The same is in `microphone` of `GET /api/info`.

## For developers

### Build the SD card image

```sh
./image/build.sh
```

This uses the official [pi-gen](https://github.com/RPi-Distro/pi-gen) at the commit pinned in `image/VERSIONS` (Raspberry Pi OS Lite, trixie, 64-bit: stages 0 to 2 plus `image/stage-wildnetwork`). It needs Docker with privileged containers and about 15 GB of free disk; `WN_PRUNE_WORK=1` saves about 3 GB. The result is `image/deploy/*.img.xz` with its SHA-256 in `*.img.xz.sha256`. The `SD card image` GitHub Actions workflow runs the tests and the same script when started by hand or when a `v*` tag is pushed (for example `git tag v0.2.0 && git push origin v0.2.0`), and attaches the image and its `.sha256` to that tag's release. Everything in `base/`, `systemd/` and `install.sh` at the tagged commit goes into the image, so a release image always has the current microphone choice, checks and services.

The image stage runs `base/provision.sh --image`, the same steps as `install.sh` without starting anything, then adds the BirdNET-Go arm64 container (downloaded with crane, checked against the pinned digest) for `wn-firstboot.service` to load.

### Finding a Base on the network (mDNS)

wnbase advertises itself with avahi as DNS-SD service type **`_wlnw-base._tcp`**, instance name = the host name (for example `wildnetwork-base-ab12`), port = wnbase's port (80), from `/etc/avahi/services/wlnw-base.service`. TXT records:

| Key | Value |
| --- | --- |
| `name` | station name from Setup (the host name until one is set), at most 64 characters |
| `model` | `wildnetwork-base` |
| `version` | wnbase version |
| `hw` | hardware id (Raspberry Pi serial number), the same as `hardwareId` in `/api/info` |
| `path` | `/api/info` |
| `test` | `yes` for a test station, else `no` |

The file is rewritten when the name or test mode changes. Look for it with `avahi-browse -rt _wlnw-base._tcp` (Linux) or `dns-sd -B _wlnw-base._tcp` (macOS). BirdNET-Go itself is on port 8080 of the same host.

### Health reports

Every 15 minutes (first one a minute after start) wnbase sends `POST <site>/api/v1/devices/<deviceId>/health` with the device key (`Authorization: Bearer`), once the Base is registered:

```json
{"at": "2026-11-19T06:00:00Z", "software": {"wnbase": "0.2.2", "wdx-agent": "0.4.6", "birdnet-go": "20260823"},
 "uptimeSeconds": 86400, "boots": 3, "temperatureC": 48.2, "storage": {"freeMb": 21000, "totalMb": 31000},
 "microphone": {"active": true, "lastDetectionAt": "2026-11-19T05:58:12Z"}, "queue": {"pending": 0},
 "network": {"type": "wifi", "signal": 67, "signalUnit": "percent"}}
```

`boots` is counted by wnbase (`/var/lib/wnbase/boots.json`). `microphone.active` is `microphone.found` of `/api/info`; `lastDetectionAt` is the newest detection of the last 7 days, or null. `network` is left out when the Base has no uplink; `signal` is the Wi-Fi or 4G signal in percent (from NetworkManager or ModemManager) and is left out for Ethernet. There is no audio, no detection content and no location in it. While the Base is offline or not registered, reports wait in `/var/lib/wnbase/health-queue.json` (at most 96, one day; older ones are dropped) and are sent together as a JSON array once it is back.

### Local API

All responses are JSON. GET endpoints allow any origin (CORS). POST endpoints need the header `X-Setup-Code`.

#### GET /api/info

No code needed.

```json
{"hardwareId": "10000000abcd1234", "model": "wildnetwork-base", "hostname": "wnbase",
 "software": {"wnbase": "0.1.0", "wdx-agent": "0.3.0", "birdnetGo": "latest"},
 "configured": true, "deviceId": "wnb_1", "deploymentId": "wildnetwork-base:29ab78f229b9", "name": "Hill top", "latitude": 51.5, "longitude": -0.12,
 "registration": "registered",
 "microphone": {"found": true, "device": ":1,0", "devices": [":1,0 (USB Lavalier Microphone, USB)"], "problem": null},
 "test": false, "uplink": {"type": "wifi", "profile": "wn-uplink", "ssid": "Farm office"},
 "timezone": "Asia/Kolkata", "timezoneSource": "location", "timezoneSetting": null, "birdnetGoTimezone": "Asia/Kolkata",
 "update": {"auto": true, "current": {"wnbase": "0.3.0", "wdx-agent": "0.4.6"}, "latest": {"wnbase": "0.3.0", "wdx-agent": "0.4.6"},
            "lastCheck": "2026-11-21T02:14:09Z", "lastResult": "up to date"}}
```

`configured` is true once the location is set. `registration` is `registered`, `waiting-for-setup` (no name or location yet), `waiting-for-internet`, or `error: <reason>` (for example too many new devices from one network in a day; it retries every hour). The key itself is never returned. `deploymentId` is the id Wildlife Network files this Base's detections under (wdx-agent's `system` and station id), so an app can ask for them with `GET <site>/api/v1/events?deployment=<deploymentId>` when it cannot reach the Base. `test` is true for a test station (see `POST /api/config`). `uplink` is the active connection that carries traffic out (`type` `wifi`, `4g` or `ethernet`, the NetworkManager `profile`, and the `ssid` for Wi-Fi), or null while the Base only runs its hotspot. `microphone.found` is `false` when BirdNET-Go has no active audio source (newest of its log lines "starting without active audio sources" and "capture started"), when `arecord -l` lists no capture device, or when the configured `device` names a card that cannot record; `null` when none of that can be checked. `problem` says which. Checked at most once a minute. `timezone` is the Base's time zone now; `timezoneSource` is `location` (looked up from the coordinates), `setting` (chosen in Setup, `wnbase.ini` or `wildnetwork-timezone.txt`; `timezoneSetting` holds it, else null) or `system` (neither applies yet, for example before setup); `birdnetGoTimezone` is the `TZ` in BirdNET-Go's service, or null without BirdNET-Go. `update` is about [automatic updates](#automatic-updates): `auto` is the switch, `current` the versions running, `latest` the newest found at the last check (null before the first), `lastCheck` when it last ran and `lastResult` what happened: `up to date`, `updated wnbase 0.2.2 -> 0.3.0`, `updated wdx-agent 0.4.6 -> 0.4.7`, `rolled back: ...`, `skipped: ...` (switched off, or no internet) or `error: ...` (nothing was changed).

**Registration.** While the agent config has a name and location but no `api_key`, wnbase calls `POST <endpoint base>/api/v1/devices` with `{"name", "model": "wildnetwork-base", "hardwareId"}` every 5 minutes (backing off to 1 hour after failures, and waiting an hour after a `429`), writes the returned `api_key`, `device_id` and `system = wildnetwork-base` to `/etc/wdx-agent.ini`, and restarts wdx-agent. Saving the setup triggers an attempt straight away. `birdnetGo` is the BirdNET-Go image tag, or null.

#### POST /api/config

Header `X-Setup-Code`. Every field is optional; only the fields sent are changed.

```json
{"name": "Hill top", "latitude": 51.5, "longitude": -0.12, "roundCoords": 2,
 "apiKey": "wn_...", "deviceId": "wnb_1", "endpoint": "https://wlnw.org/api/v1/events",
 "apn": "internet", "wifi": {"ssid": "Farm office", "psk": "secret123"}, "test": false, "timezone": "Asia/Kolkata", "autoUpdate": true}
```

`autoUpdate` (default on) switches [automatic updates](#automatic-updates) on or off: `auto_update = yes|no` under `[base]` in `/etc/wnbase/wnbase.ini`.

`timezone` (optional) chooses the time zone instead of the one of the location: an IANA name, or `""` to go back to the location's. It is stored as `[base] timezone` in `/etc/wnbase/wnbase.ini`. When the location or `timezone` changes, the Base applies the zone (see [Time zone](#first-time-setup)) and the response has `"timezone"`.

`test` (default off) makes this a **test station**: `test = yes` in `/etc/wdx-agent.ini`, so wdx-agent (0.4.4 or later) adds the top-level field `"test": true` to every event, and Wildlife Network keeps those events apart and never shows them publicly. Each change is also sent to Wildlife Network as `POST <site>/api/v1/devices/<deviceId>/settings` with `{"test": true|false}` and the device key (`Authorization: Bearer`). It waits in `/var/lib/wnbase/pending-settings.json` while the Base is offline or not registered yet, and is retried every 2 minutes until it is accepted.

Writes `/etc/wdx-agent.ini` (other keys are kept) and restarts wdx-agent. A new `latitude`/`longitude` is also written, unrounded, into BirdNET-Go's `config.yaml` (only the `latitude` and `longitude` keys directly under `birdnet:` change; the rest of the file is kept as it was), and the `birdnet-go` container is restarted, so BirdNET-Go's species range filter uses the real location. At 0,0 it would accept species that do not occur there. The exact location stays on the device; `roundCoords` only applies to what wdx-agent uploads. `apn` creates or updates the NetworkManager connection `wn-4g`. `wifi` writes the Base's one home Wi-Fi profile, `wn-uplink` (created or updated), and switches off (`autoconnect no`) every other Wi-Fi client profile on `wlan0`, such as the `netplan-wlan0-...` one Raspberry Pi Imager makes, so only one profile competes for the radio. 4G and Ethernet profiles are not touched. `wn-net` moves to the new network once no phone is on the hotspot (within 5 minutes), or at the next boot. Response:

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

#### POST /api/birdnet-go-password

Header `X-Setup-Code`. Returns the password of BirdNET-Go's settings page made by this Base, `{"password": "...", "url": ":8080"}`, or `404` when BirdNET-Go has a password set by its owner instead.

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
| `base/wnnet.py` | Home Wi-Fi first, hotspot as fallback (`wn-net.service`) |
| `base/hotspot.sh` | Starts the hotspot now, by hand (`/opt/wnbase/hotspot.sh`) |
| `base/wnupdate.py` | Automatic updates of the Base software and wdx-agent, with rollback (`/opt/wnbase/wnupdate.py`, `wnbase-update.service`) |
| `install.sh` | Installer for a stock Raspberry Pi |
| `base/provision.sh` | Steps shared by `install.sh` and the image build |
| `base/bngauth.py` | Turns on BirdNET-Go's login with a password of this Base (`/opt/wnbase/bngauth.py`) |
| `base/tzgrid.py`, `base/tzgrid.dat` | Offline time zone of a location (`/opt/wnbase/`): one IANA zone per 0.05 degree cell, gzip, 190 KB, standard library only |
| `tools/build-tzgrid.py` | Rebuilds `base/tzgrid.dat` from [timezonefinder](https://github.com/jannikmi/timezonefinder) on a development machine (after a time zone border changes) |
| `base/mic.py` | Picks BirdNET-Go's microphone (`/opt/wnbase/mic.py`); also used by wnbase for the microphone check |
| `base/birdnet-go-check.sh` | Checks that BirdNET-Go's installer finished (used by `install.sh`) |
| `base/setup-file.sh` | Writes `wildnetwork-setup.txt` to the boot partition (first boot and `install.sh`) |
| `base/firstboot.sh`, `base/ssh-key.sh`, `base/birdnet-go-unit.sh` | Image only: first boot, SSH from the boot partition, BirdNET-Go unit (its `TZ` is the host's zone; wnbase changes it with the zone) |
| `image/` | SD card image: `build.sh`, `VERSIONS`, pi-gen stage |
| `systemd/` | `wnbase.service`, `wn-net.service`, `wdx-agent.service`, `wnbase-update.service` with `wnbase-update.timer` (nightly) and `wnbase-update-boot.timer` (after boot), `wn-firstboot.service`, `wn-ssh-key.service` |
| `/var/lib/wnbase/update.json` | Last update check: `lastCheck`, `lastResult`, `latest` |
| `/opt/wnbase.prev`, `/opt/wdx-agent/wdx_agent.py.prev` | The version before the last automatic update |
| `tests/` | `python3 -m unittest discover -s tests` (needs wdx-agent checked out next to this repo, or `WDX_AGENT_PY`; with timezonefinder installed, `test_timezone` also compares the bundled map with it) |

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
# BirdNET-Go's service, whose TZ follows the Base's time zone
birdnet_unit = /etc/systemd/system/birdnet-go.service

[base]
# IANA time zone such as Asia/Kolkata; empty: looked up from the station location
timezone =
# no: do not install new versions of the Base software and wdx-agent by themselves
auto_update = yes

[hotspot]
ifname = wlan0
ssid_prefix = Wildlife Network-
force = no
boot_wait_seconds = 90
retry_seconds = 300
fallback_seconds = 180
```

## Part of an open wildlife toolkit

Eight open source projects for listening to, identifying and mapping wildlife. They work together, but you rarely need more than one or two. Start from what you want to do:

| I want to | Use | What it is |
|---|---|---|
| See where birds and wildlife are moving, or download the data | [Wildlife Network](https://github.com/wlnw-org/wildnetwork) | The live map and open data: [wlnw.org](https://wlnw.org) |
| Build a monitoring station from open hardware | [Wildlife Network Base](https://github.com/arunrajiah/wildnetwork-base) (this project) | Software and a ready-to-flash SD card image (beta) for the open Wildlife Network station (Raspberry Pi, microphone, solar) |
| Share detections from a BirdNET-Pi, BirdNET-Go, camera trap or bat detector you already have | [wdx-agent](https://github.com/arunrajiah/wdx-agent) | One small program that sends your station's detections. BirdWeather stations are already included and need nothing |
| Follow your own station on your phone | [BirdEcho](https://github.com/arunrajiah/birdecho) | Android app for BirdNET-Pi, BirdNET-Go and BirdWeather stations |
| Identify a sound you just heard | [WildEcho](https://github.com/arunrajiah/wildecho) | Phone app: record a clip, get ranked species |
| Run your own sound identification server | [wildecho-api](https://github.com/arunrajiah/wildecho-api) | Self-hosted species identification from audio, on CPU, no API keys |
| Check camera trap predictions before you use them | [SpeciesNet Studio](https://github.com/arunrajiah/speciesnet-studio) | Self-hosted review of SpeciesNet results, then send them to Wildlife Network |
| Make your own software or device produce or read detections in a common format | [WDX](https://github.com/arunrajiah/wildlife-detection-exchange) | The open format for one AI wildlife detection; maps to Darwin Core |

**How this one fits.** The Wildlife Network Base is the open station. It identifies birds on the device with BirdNET-Go, sends detections to Wildlife Network through wdx-agent, and is set up from a phone or a browser.

**How they connect:** stations (a Wildlife Network Base, BirdNET-Pi, BirdNET-Go, camera traps) produce detections; wdx-agent sends them in the WDX format, SpeciesNet Studio adds a human check for camera traps, and Wildlife Network maps them. Connected today: wdx-agent, the Wildlife Network Base, SpeciesNet Studio and WildEcho (if you switch sharing on) send to Wildlife Network, BirdEcho shows Wildlife Network arrivals near your station, and WildEcho uses wildecho-api.

## Licence

Apache-2.0. See `LICENSE`.

## Name and logo

The code is open source (Apache-2.0). The name "Wildlife Network" and the logo are not part of the code licence: forks that run as a public service or ship devices should use their own name and logo. See [TRADEMARKS.md](TRADEMARKS.md).
