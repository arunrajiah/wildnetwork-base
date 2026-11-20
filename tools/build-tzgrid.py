#!/usr/bin/env python3
"""Build base/tzgrid.dat, the offline time zone map of the Base (read by base/tzgrid.py, standard library only).

Runs on a development machine, not on the Base:
  python3 -m venv /tmp/tzv && /tmp/tzv/bin/pip install timezonefinder numpy
  /tmp/tzv/bin/python tools/build-tzgrid.py [resolution degrees, default 0.05] [output, default base/tzgrid.dat]

Each grid cell gets the IANA zone at its centre, from timezonefinder (timezone-boundary-builder polygons). A cell
whose centre is at sea takes the zone of the nearest land cell within about 30 km, so a station on the coast gets
its country's zone and not a nautical Etc/GMT zone; further out at sea the nautical zone stays.
File: gzip of "res rows cols source" / zone names separated by spaces / one line per row (south to north) of
comma separated runs "zone index[*run length]" from 180 W eastwards.
"""
import gzip
import sys
from pathlib import Path

import numpy as np
from timezonefinder import TimezoneFinder
from importlib.metadata import version

res = float(sys.argv[1]) if len(sys.argv) > 1 else 0.05
out_path = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "base" / "tzgrid.dat")
tf = TimezoneFinder(in_memory=True)
rows, cols = int(round(180 / res)), int(round(360 / res))
names: dict[str, int] = {}


def zid(name: str) -> int:
    return names.setdefault(name, len(names))


land = np.full((rows, cols), -1, np.int16)
for r in range(rows):
    lat = -90 + (r + 0.5) * res
    for c in range(cols):
        z = tf.timezone_at_land(lat=lat, lng=-180 + (c + 0.5) * res)
        if z:
            land[r, c] = zid(z)

grid = land.copy()
reach = max(1, int(round(0.3 / res)))
for k in range(1, reach + 1):
    ring = [(dr, dc) for dr in range(-k, k + 1) for dc in range(-k, k + 1) if max(abs(dr), abs(dc)) == k]
    for dr, dc in ring:
        shifted = np.roll(np.roll(land, dr, axis=0), dc, axis=1)  # longitude wraps; latitude wrap only touches the poles
        fill = (grid < 0) & (shifted >= 0)
        grid[fill] = shifted[fill]
for r, c in np.argwhere(grid < 0):
    grid[r, c] = zid(tf.timezone_at(lat=-90 + (r + 0.5) * res, lng=-180 + (c + 0.5) * res) or "Etc/UTC")

zones = sorted(names, key=names.get)
lines = []
for r in range(rows):
    row = grid[r]
    cut = np.flatnonzero(np.diff(row)) + 1
    starts, ends = np.concatenate(([0], cut)), np.concatenate((cut, [cols]))
    lines.append(",".join(f"{row[a]}" + (f"*{b - a}" if b - a > 1 else "") for a, b in zip(starts, ends)))
source = f"timezonefinder-{version('timezonefinder')}/data-{version('timezonefinder-data')}"
body = f"{res} {rows} {cols} {source}\n" + " ".join(zones) + "\n" + "\n".join(lines) + "\n"
out_path.write_bytes(gzip.compress(body.encode(), 9, mtime=0))
print(f"{out_path}: {len(zones)} zones, {rows}x{cols} cells, {out_path.stat().st_size} bytes ({source})")
