"""Check every station against the county polygon its own metadata claims.

`web/src/lib/taiwan.ts` is generated from the official boundary file, then
simplified by three orders of magnitude to be drawn at 290px. Simplification is
lossy in a way that can be wrong rather than merely coarse: a boundary pushed
the wrong side of a station puts that station in the neighbouring county, and on
a map whose entire job is "which county is that dot in", that is a factual
error, not a rendering artefact.

So the test is not "does the outline look like Taiwan". It is:

    every monitoring station falls inside the polygon of the county that
    MOENV's own register says it belongs to

which is 73 independent assertions against a source this repository does not
control. Both sides can move — the geometry when the generator is re-run at a
different tolerance, the station list when MOENV publishes a new register — so
CI runs this on every push.

It reads the shipped TypeScript rather than a copy of the coordinates. A
validator checking its own duplicate passes forever while the site drifts.

    python scripts/validate_map_geometry.py
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
GEO_TS = ROOT / "web" / "src" / "lib" / "taiwan.ts"
META = ROOT / "web" / "public" / "data" / "meta.json"

# Matches the frame the component draws, so offshore counties are excluded here
# exactly as they are excluded there.
MARGIN = 0.04

Ring = list[tuple[float, float]]


def read_counties() -> dict[str, list[Ring]]:
    """Pull `{county name: [ring, ...]}` out of the shipped module."""
    text = GEO_TS.read_text(encoding="utf-8")
    body = text.split("export const COUNTIES", 1)[1]

    counties: dict[str, list[Ring]] = {}
    for block in re.finditer(r'name:\s*"([^"]+)".*?rings:\s*\[(.*?)\n    \],', body, re.S):
        name, rings_src = block.group(1), block.group(2)
        rings: list[Ring] = []
        for ring_src in re.findall(r"\[\[.*?\]\]", rings_src, re.S):
            rings.append([tuple(p) for p in ast.literal_eval(ring_src)])
        counties[name] = rings
    return counties


def inside(px: float, py: float, rings: list[Ring]) -> bool:
    """Even-odd containment across *all* of a county's rings at once.

    Not one ring at a time. Two counties are true enclaves, so their neighbour's
    ring list carries a hole: a point in 臺北市 crosses 新北市's exterior once
    and its interior ring once and comes out even, which is "outside". The first
    version of this file asked each ring separately and OR-ed the answers, which
    reports that point as inside both counties — and, worse, is structurally
    incapable of ever noticing, so it passed while the shipped map had 嘉義市
    painted over by 嘉義縣.

    This is the same rule the map fills with (`fill-rule: evenodd`), so the
    check and the drawing cannot disagree.
    """
    hit = False
    for ring in rings:
        for i in range(len(ring)):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % len(ring)]
            # The straddle test has to come first and short-circuit: without it
            # the division is by zero on every horizontal edge.
            straddles = (y1 > py) != (y2 > py)
            if straddles and px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
                hit = not hit
    return hit


def main() -> int:
    counties = read_counties()
    if len(counties) < 15:
        print(f"county data looks truncated: {len(counties)} counties", file=sys.stderr)
        return 1

    every_point = [p for rings in counties.values() for r in rings for p in r]
    lons = [p[0] for p in every_point]
    lats = [p[1] for p in every_point]
    frame = (
        min(lons) - MARGIN,
        max(lons) + MARGIN,
        min(lats) - MARGIN,
        max(lats) + MARGIN,
    )

    stations = json.loads(META.read_text(encoding="utf-8"))["stations"]
    drawn = [
        s
        for s in stations
        if s.get("lat") is not None
        and s.get("lon") is not None
        and frame[0] <= s["lon"] <= frame[1]
        and frame[2] <= s["lat"] <= frame[3]
    ]

    wrong: list[str] = []
    unknown: list[str] = []
    overlapping: list[str] = []
    for station in drawn:
        claimed = station.get("county")
        if claimed not in counties:
            unknown.append(f"{station['station_name']} claims {claimed!r}")
            continue
        holding = [
            name
            for name, rings in counties.items()
            if inside(station["lon"], station["lat"], rings)
        ]
        if claimed not in holding:
            wrong.append(
                f"{station['station_name']} claims {claimed} but falls outside it "
                f"({station['lon']:.4f}E {station['lat']:.4f}N)"
                + (f", found in {holding}" if holding else "")
            )
        elif len(holding) > 1:
            # The check that the enclave defect needed. Asking only "is it in
            # its own county" can never see two counties claiming one point.
            overlapping.append(f"{station['station_name']} is inside {len(holding)}: {holding}")

    print(f"counties       : {len(counties)}")
    print(f"boundary points: {len(every_point)}")
    print(f"stations drawn : {len(drawn)}")
    print(
        f"exactly one    : {len(drawn) - len(wrong) - len(unknown) - len(overlapping)}/{len(drawn)}"
    )
    for line in unknown + wrong + overlapping:
        print(f"  {line}")

    return 1 if (wrong or unknown or overlapping) else 0


if __name__ == "__main__":
    raise SystemExit(main())
