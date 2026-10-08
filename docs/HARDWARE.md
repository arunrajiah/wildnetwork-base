<img src="../assets/logo.svg" width="64" alt="WildNetwork">

# Build a WildNetwork Base

This guide takes you from a box of parts to a Base that is listening, identifying birds and showing up on the [WildNetwork](https://wildnetwork.arunrajiah.com) map. No soldering and no programming are needed. Plan on about an hour for an indoor Base and half a day for an outdoor, solar-powered one.

There are three ways to build one. Pick the row that matches where the Base will live, then follow the steps.

| Build | Where | Power | Internet |
|---|---|---|---|
| **A. Indoor or sheltered** | A window sill, balcony, verandah, garden shed | Wall socket | Your Wi-Fi |
| **B. Outdoor with mains power** | A garden, campus, farm building | Wall socket through an outdoor cable | Your Wi-Fi, or a 4G SIM |
| **C. Remote, off grid** | A reserve, forest edge, field station | Solar panel and battery | 4G SIM, or a phone collects the data on visits |

## 1. Parts

### Every build

| Part | What to look for | Approx. cost |
|---|---|---|
| Raspberry Pi 4 Model B (2 GB or more) or Raspberry Pi 5 | Buy from an official reseller. The Pi 4 uses less power, which matters for solar. | ₹4,500 to 7,500 (US$50 to 85) |
| microSD card, 32 GB or larger | A known brand, "A1" or "A2" rated. Cheap cards fail after months of constant writing. | ₹400 to 800 (US$5 to 9) |
| USB microphone | See [Choosing a microphone](#choosing-a-microphone). | ₹1,000 to 4,000 (US$11 to 45) |
| Power supply | Pi 4: official 5.1 V 3 A USB-C. Pi 5: official 5.1 V 5 A (27 W) USB-C. Phone chargers often cause random restarts. | ₹700 to 1,200 (US$8 to 14) |
| Case or heatsink for the Pi | A case with a fan, or a large aluminium heatsink case. It keeps the Pi cool in summer. | ₹500 to 1,200 (US$6 to 14) |
| Computer with an SD card reader | To flash the card once. Any Windows, macOS or Linux computer. | (you have it) |

### Add for outdoor builds (B and C)

| Part | What to look for | Approx. cost |
|---|---|---|
| Weatherproof enclosure | IP65 or better ABS junction box, light coloured, about 200 × 150 × 100 mm or larger. | ₹600 to 1,500 (US$7 to 17) |
| Cable glands | PG7 or PG9 nylon glands, one per cable that goes through the box. | ₹100 to 300 (US$1 to 3) |
| Breather vent | A small vent plug (often sold as "Gore vent" or "IP68 breather"). Stops condensation building up inside. | ₹150 to 400 (US$2 to 5) |
| Silica gel packs | A few sachets inside the box, replaced at each visit. | ₹50 (US$1) |
| Microphone shield | A short length of PVC pipe or a small plastic cup to keep rain off the microphone, and a foam windscreen. | ₹100 to 300 (US$1 to 3) |
| Mounting | Pole clamps, a bracket or stainless steel cable ties. | ₹200 to 600 (US$2 to 7) |

### Add for 4G (B or C without Wi-Fi)

| Part | What to look for | Approx. cost |
|---|---|---|
| USB 4G modem | Easiest: a "HiLink" USB dongle (for example Huawei E3372h); it appears to the Pi as a network cable and needs no setup on the Base. Alternative: a Quectel EC25 USB modem, which the Base sets up with your SIM's APN. | ₹2,000 to 4,000 (US$25 to 45) |
| Data SIM | Any operator with coverage at the site. A Base sends only a few kilobytes a day, so the smallest data plan is enough. | Plan dependent |

### Add for solar (C)

A Pi 4 running BirdNET-Go draws about 3 to 5 W around the clock, which is roughly 100 Wh a day. Size the system for the cloudiest week of the year, not for a sunny day.

| Part | What to look for | Approx. cost |
|---|---|---|
| Solar panel | 50 W, 12 V, monocrystalline. 30 W can work in sunny months but often runs out during the monsoon. | ₹2,000 to 3,500 (US$25 to 40) |
| Battery | 12 V LiFePO4, 20 Ah (about 256 Wh, two to three days without sun). Avoid ordinary lead acid in hot places. | ₹5,000 to 8,000 (US$55 to 90) |
| Solar charge controller | 10 A, with a LiFePO4 setting. MPPT is better than PWM but costs more. | ₹800 to 3,000 (US$9 to 35) |
| 12 V to 5 V converter | A step-down (buck) converter with a USB-C output: 5 V 3 A for a Pi 4, 5 V 5 A for a Pi 5. | ₹400 to 900 (US$5 to 10) |
| Fuse and holder | An inline 5 A fuse on the battery's positive wire. | ₹100 (US$1) |
| Wire | 1.5 mm² (or 16 AWG) red and black, with ring terminals for the battery. | ₹200 (US$2) |

Prices are rough estimates from Indian retail listings in 2026, not quotes; US dollar figures are converted at about ₹88 to US$1 and rounded, and prices outside India will differ. A complete solar Base costs roughly ₹15,000 to ₹25,000 (US$170 to 285) depending on the parts you choose.

### Choosing a microphone

The microphone matters more than any other part: a good one hears birds two or three times farther away.

- **Good:** an omnidirectional USB microphone, or a USB sound card with an external microphone. Microphones built on the Primo EM272 capsule are widely used by BirdNET-Pi and BirdNET-Go stations.
- **Usable for a start:** a USB lavalier (clip-on) microphone.
- **Avoid:** webcams, headsets, and microphones with automatic noise cancelling: they filter out bird song.
- Use one microphone per Base. If several sound devices are connected, BirdNET-Go uses the first one it finds.

## 2. Flash the SD card

1. On your computer, install [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Download the newest `wildnetwork-base` `.img.xz` file from [Releases](https://github.com/arunrajiah/wildnetwork-base/releases).
3. Put the microSD card in your computer. In Imager, choose your Raspberry Pi model, then **Operating System > Use custom**, pick the downloaded file, pick the card, and write it. Skip Imager's "OS customisation" step: the WildNetwork image does not use it.
4. Optional: to log in over SSH later, open the card's `bootfs` drive and add a file named `wildnetwork-ssh.pub` containing your SSH public key.

## 3. Test it on a table first

Always test indoors before you climb a pole.

1. Put the card in the Pi. Plug in the microphone (and the 4G modem with its SIM, if you use one).
2. Connect the power supply. The red light comes on, and the green light flickers while it starts.
3. Wait about 2 minutes. The first start takes longer than later ones.
4. On your phone, open the Wi-Fi settings and join **WildNetwork-XXXX**. The password is printed on the label as `wn-` followed by the setup code. No label yet? The setup code is also in the file `wildnetwork-setup.txt` on the card's `bootfs` drive, and on the screen if you plug in a monitor.
5. Open **http://10.42.0.1** in the phone's browser. You should see the WildNetwork Base dashboard.
6. Play a bird recording from another phone or speaker near the microphone. Within a minute or two it should appear under recent detections. BirdNET-Go's own page at http://10.42.0.1:8080 shows the live sound level, which tells you the microphone is working.

If nothing appears, see [Troubleshooting](#7-troubleshooting).

## 4. Set it up

Do this while the Base is still on the table.

**With the WildNetwork field app** (BirdEcho): choose "WildNetwork Base" and follow the steps.

**With any browser:** on the dashboard, open **Setup** and enter:

1. The **setup code** from the label.
2. A **name** for the station, for example "Hill top school". It is shown publicly, so do not use a person's name or a home address.
3. The **location**. Use the phone's location or type the coordinates. The exact position stays on the Base so BirdNET-Go can rule out birds that do not occur there; WildNetwork only receives it rounded to about 1 km.
4. **How it reaches the internet:** your Wi-Fi network and password, or your SIM's APN for a 4G modem (common APNs in India: Jio `jionet`, Airtel `airtelgprs.com`, Vi `www`; check with your operator). HiLink dongles need no APN here. Leave it empty if a phone will collect the data on visits.

Once it has internet, the Base registers itself with WildNetwork and starts sending. Within about 15 minutes it appears under **Device health** on the [stations page](https://wildnetwork.arunrajiah.com/stations).

## 5. Build the outdoor enclosure

Skip this for an indoor Base.

```
                 ┌──────────────── enclosure (lid up) ─────────────────┐
                 │                                                      │
  solar panel ───┼─▶ charge controller ──▶ battery (fused + wire)      │
                 │          │                                           │
                 │          └──▶ 12 V to 5 V converter ──▶ Raspberry Pi │
                 │                                          │    │      │
                 │                               4G modem ──┘    │      │
                 │                                                │      │
                 └──────────── cable gland ───────────────────────┼──────┘
                                                                  │ USB cable
                                                      microphone, pointing down,
                                                      under a rain shield
```

1. **Plan the layout.** Lay the parts inside the open box: the Pi at the top (heat rises away from the battery), the battery at the bottom, and the controller and converter in between. Keep 2 to 3 cm of air around the Pi.
2. **Drill the holes in the bottom** of the box, never the top: one for each cable gland (solar cable, microphone cable) and one for the breather vent. Holes on the bottom keep rain out.
3. **Fit the glands and the vent**, and tighten them by hand plus a quarter turn.
4. **Mount the parts** with double-sided mounting tape or small screws into the box's mounting plate.
5. **Wire the power** (solar build). Connect in this order, so the controller recognises the battery voltage:
   1. battery to the charge controller (with the fuse on the positive wire);
   2. solar panel to the controller;
   3. the 12 V to 5 V converter to the controller's load output (or directly to the battery, through the fuse);
   4. the converter's USB-C to the Pi.
   
   Red is positive, black is negative. Check twice before connecting the battery.
6. **Run the microphone cable** through its gland and fit the microphone outside, pointing down, under the rain shield, with the foam windscreen on. Leave a small drip loop in the cable below the gland so water runs off instead of into the box.
7. **Add the silica gel packs** and close the lid.

## 6. Install it outdoors

1. **Height:** 2 to 4 metres up, on a pole, a tree (with a strap, not nails) or a wall bracket.
2. **Away from noise:** at least 20 metres from roads, generators, water pumps and air conditioners if you can. Noise hides bird song.
3. **Shade the box, sun the panel:** the Pi slows down above about 80 °C. Put the box on the shaded side of the pole or under a small roof, and point the panel south (in India and the northern hemisphere), tilted at about your latitude.
4. **Microphone in the open:** not pressed against a wall or inside foliage. Sound should reach it from all sides.
5. **Check the 4G signal** at the exact spot with a phone on the same operator before you fix the box.
6. **After 24 hours,** look at the Base on the [stations page](https://wildnetwork.arunrajiah.com/stations): its last report time, battery and storage. Detections from it appear on the map.

**On each visit** (every one to three months): wipe the panel, replace the silica gel, check the cable glands and the microphone windscreen, and look for ants or wasps in the box. If the Base has no 4G, open the field app near it so the phone collects the waiting detections.

## 7. Troubleshooting

| What you see | Likely cause | What to do |
|---|---|---|
| No WildNetwork-XXXX Wi-Fi after 3 minutes | The card was not written correctly, or the Base joined a known Wi-Fi network | Flash the card again. If it joined your Wi-Fi, open http://wildnetwork-base-XXXX.local on that network instead. |
| The Pi restarts on its own | Power supply too weak, or the battery is low | Use the official supply. On solar, check the battery voltage and the converter rating. |
| No detections after playing bird sounds | Microphone not found, or too quiet | Open http://10.42.0.1:8080 and check the sound level. Try another USB port; unplug other sound devices. |
| Many wrong species | The location is not set, or the microphone picks up a lot of noise | Set the location in Setup. Move the Base away from noise. |
| Detections stay on the Base | No internet | Check the APN or Wi-Fi in Setup, the SIM's data balance, or the signal. A phone with the field app can collect the data meanwhile. |
| "Will connect to WildNetwork when the Base has internet" never changes | Internet is not reaching the Base | Same as above; the Base retries on its own. |
| Hot Base, slow detections in summer | Overheating | Add shade, a fan case or a bigger heatsink; move the box out of direct sun. |
| Lost the setup code | | It is in `wildnetwork-setup.txt` on the card's `bootfs` drive. |

Still stuck? Open an issue on [GitHub](https://github.com/arunrajiah/wildnetwork-base/issues) or write to arunrajiah@gmail.com with a photo of your build and what the dashboard shows.

## Safety

- Use a fuse on the battery's positive wire. A short circuit from a lithium battery can start a fire.
- Do not work on mains wiring yourself: use a ready-made weatherproof outdoor extension or ask an electrician.
- Get permission before installing on land you do not own, and from the forest department before installing inside a protected area.
