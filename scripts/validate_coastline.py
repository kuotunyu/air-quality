"""Check that every station the map plots falls inside the coastline it draws.

The outline in `web/src/lib/taiwan.ts` is hand-encoded rather than lifted from a
boundary file: the only shapefile available offline here is Natural Earth
1:110m, which gives Taiwan nine points and reads as a blob, and carrying a real
one would mean a dependency and a licence to track for what is, at 290px wide, a
locator silhouette.

So the outline has to earn trust some other way, and this is it. The stations
the map draws run the length of the island and sit on both coasts; an outline
drawn inland of any of them puts a monitoring station in the sea. That is a test
the coastline can fail, and on its first run it failed four times.

This reads the shipped TypeScript rather than a copy of the coordinates. A
validator checking its own duplicate of the data would pass forever while the
site drifted.

    python scripts/validate_coastline.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COAST_TS = ROOT / "web" / "src" / "lib" / "taiwan.ts"
META = ROOT / "web" / "public" / "data" / "meta.json"

# The frame the map uses, so offshore sites are excluded here exactly as they
# are excluded there.
MARGIN = 0.08


def read_coast() -> list[tuple[float, float]]:
    """Pull the [lon, lat] pairs out of the shipped module."""
    text = COAST_TS.read_text(encoding="utf-8")
    body = text.split("TAIWAN_COAST", 1)[1].split("];", 1)[0]
    pairs = re.findall(r"\[\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*\]", body)
    return [(float(lon), float(lat)) for lon, lat in pairs]


def inside(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    """Ray casting. True when the point lies within the ring."""
    hit = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        # The straddle test has to come first and short-circuit: without it the
        # division is by zero on every horizontal edge.
        straddles = (y1 > py) != (y2 > py)
        if straddles and px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def main() -> int:
    coast = read_coast()
    if len(coast) < 20:
        print(f"coastline looks truncated: {len(coast)} points", file=sys.stderr)
        return 1

    lons = [p[0] for p in coast]
    lats = [p[1] for p in coast]
    bounds = (
        min(lons) - MARGIN,
        max(lons) + MARGIN,
        min(lats) - MARGIN,
        max(lats) + MARGIN,
    )

    stations = json.loads(META.read_text(encoding="utf-8"))["stations"]
    plotted = [
        s
        for s in stations
        if s.get("lat") is not None
        and s.get("lon") is not None
        and bounds[0] <= s["lon"] <= bounds[1]
        and bounds[2] <= s["lat"] <= bounds[3]
    ]

    outside = [s for s in plotted if not inside(s["lon"], s["lat"], coast)]

    print(f"coastline points   : {len(coast)}")
    print(f"stations on the map: {len(plotted)}")
    print(f"outside the outline: {len(outside)}")
    for station in outside:
        print(f"  {station['station_name']} at {station['lon']:.3f}E {station['lat']:.3f}N")

    return 1 if outside else 0


if __name__ == "__main__":
    raise SystemExit(main())
