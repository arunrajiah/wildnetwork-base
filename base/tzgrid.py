"""Offline time zone lookup for the Wildlife Network Base: latitude and longitude to an IANA zone name.

Reads tzgrid.dat next to this file (built by tools/build-tzgrid.py from timezonefinder's polygons): one zone per
grid cell of 0.05 degrees (about 5.5 km). Standard library only, no network. Near a border the answer can be the
neighbouring zone for points within about one cell (a few km) of it; coastal cells take the zone of the land nearby.
"""
from __future__ import annotations

import gzip
import os
import threading

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tzgrid.dat")


class TzGrid:
    def __init__(self, path: str = DATA):
        with gzip.open(path, "rt", encoding="ascii") as f:
            head = f.readline().split()
            self.res, self.rows, self.cols = float(head[0]), int(head[1]), int(head[2])
            self.source = head[3] if len(head) > 3 else ""
            self.zones = f.readline().split()
            self._lines = f.read().split("\n")
        self._rows: dict[int, list[tuple[int, int]]] = {}

    def _row(self, r: int) -> list[tuple[int, int]]:
        runs = self._rows.get(r)
        if runs is None:
            runs, end = [], 0
            for part in self._lines[r].split(","):
                z, _, n = part.partition("*")
                end += int(n) if n else 1
                runs.append((end, int(z)))
            self._rows[r] = runs
        return runs

    def lookup(self, lat: float, lon: float) -> str:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("latitude or longitude out of range")
        r = min(int((lat + 90) / self.res), self.rows - 1)
        c = min(int((((lon + 180) % 360)) / self.res), self.cols - 1)
        for end, z in self._row(r):
            if c < end:
                return self.zones[z]
        raise ValueError(f"tzgrid row {r} is short")


_grid: TzGrid | None = None
_lock = threading.Lock()


def timezone_at(lat: float, lon: float) -> str:
    """IANA zone name for a point, e.g. 13.08, 80.27 -> "Asia/Kolkata"."""
    global _grid
    with _lock:
        if _grid is None:
            _grid = TzGrid()
    return _grid.lookup(float(lat), float(lon))
