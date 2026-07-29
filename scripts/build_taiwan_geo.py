"""Turn the official county boundary file into the map's geometry module.

Source
------
內政部國土測繪中心, 直轄市、縣市界線(TWD97經緯度), data.gov.tw dataset 7442,
`COUNTY_MOI_1140318_.zip` (GML, EPSG:3824, 1:5000). Licence: 政府資料開放授權條款
第1版, which permits reuse with attribution — the attribution is in the map's
own caption and in the header of the generated module.

This is a one-off generator, not part of the build. The 12.7 MB GML is *not*
committed; the simplified output is. Re-run it only when the boundaries change:

    python scripts/build_taiwan_geo.py path/to/COUNTY_MOI_1140318.gml

Why the output is simplified
----------------------------
1:5000 is roughly a metre per point. The map is at most 390px across for an
island 1.85° wide, so one pixel is about 530 m — three orders of magnitude
coarser than the source. Shipping the raw geometry would be ~12 MB of JSON to
draw something a reader cannot see. The tolerance below is chosen so the largest
displacement is a fraction of a pixel at the largest size the map is ever drawn.

Shared borders are simplified independently per county, which can in principle
open slivers between neighbours. At this tolerance a sliver is at most a third
of a pixel, and every county is stroked as well as filled, so the stroke covers
it. Checked visually and by the station test.
"""

from __future__ import annotations

import json
import pathlib
import struct
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "src" / "lib" / "taiwan.ts"

# The window the map draws. Offshore counties (Kinmen, Matsu, Penghu) have no
# geometry inside it and are reported separately by the component, exactly as
# their stations already were.
MAIN = (119.9, 122.1, 21.8, 25.5)

# Degrees. About 165 m; a third of a pixel at the widest the map is ever drawn.
TOLERANCE = 0.0015

# Rings smaller than this are offshore rocks and sandbars that read as noise at
# this size. In square degrees; about 1.2 km².
MIN_RING_AREA = 1.0e-4


# --------------------------------------------------------------------------
# WKB
# --------------------------------------------------------------------------
def parse_wkb(blob: bytes) -> list[list[tuple[float, float]]]:
    """Return every exterior ring in a (Multi)Polygon. Holes are dropped.

    Taiwan's county polygons contain no true holes — an enclave would be a
    separate feature — and a hole at this scale would be smaller than a pixel.
    """
    rings: list[list[tuple[float, float]]] = []
    pos = 0

    def take(fmt: str) -> tuple[Any, ...]:
        nonlocal pos
        size = struct.calcsize(fmt)
        out = struct.unpack_from(fmt, blob, pos)
        pos += size
        return out

    def read_polygon(endian: str) -> None:
        (n_rings,) = take(endian + "I")
        for index in range(n_rings):
            (n_points,) = take(endian + "I")
            coords = take(endian + f"{n_points * 2}d")
            if index == 0:
                rings.append(list(zip(coords[0::2], coords[1::2], strict=True)))

    (byte_order,) = take("B")
    endian = "<" if byte_order == 1 else ">"
    (geom_type,) = take(endian + "I")

    if geom_type % 1000 == 3:
        read_polygon(endian)
    elif geom_type % 1000 == 6:
        (n_polys,) = take(endian + "I")
        for _ in range(n_polys):
            (sub_order,) = take("B")
            sub_endian = "<" if sub_order == 1 else ">"
            take(sub_endian + "I")
            read_polygon(sub_endian)
    else:
        raise ValueError(f"unexpected WKB geometry type {geom_type}")

    return rings


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------
def ring_area(ring: list[tuple[float, float]]) -> float:
    """Unsigned shoelace area, in square degrees."""
    total = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def simplify(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker, iterative so a 60,000-point ring cannot blow the stack."""
    if len(points) < 3:
        return points

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        ax, ay = points[start]
        bx, by = points[end]
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy

        worst, worst_at = -1.0, start
        for i in range(start + 1, end):
            px, py = points[i]
            if span == 0:
                dist = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span))
                cx, cy = ax + t * dx, ay + t * dy
                dist = (px - cx) ** 2 + (py - cy) ** 2
            if dist > worst:
                worst, worst_at = dist, i

        if worst > tol * tol:
            keep[worst_at] = True
            stack.append((start, worst_at))
            stack.append((worst_at, end))

    return [p for p, k in zip(points, keep, strict=True) if k]


def inside(px: float, py: float, ring: list[tuple[float, float]]) -> bool:
    hit = False
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        straddles = (y1 > py) != (y2 > py)
        if straddles and px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def label_anchors(ring: list[tuple[float, float]], k: int = 4) -> list[tuple[float, float]]:
    """Several places a name could go, roomiest first.

    One anchor is not enough. The centroid is no good to begin with — Chiayi
    wraps around Chiayi City and Taitung is a crescent, so their centroids fall
    in the sea — and even the widest interior chord fails when two neighbours
    are both widest along the same edge: Kaohsiung and Pingtung are adjacent and
    both open out onto the same southern plain, so whichever is placed first
    takes the space and the other loses its label entirely.

    Offering the caller a short list of well-separated candidates lets it fall
    back to the county's second-roomiest spot instead of giving up.
    """
    lats = [p[1] for p in ring]
    lo, hi = min(lats), max(lats)
    edges = list(zip(ring, ring[1:] + ring[:1], strict=True))

    chords: list[tuple[float, float, float]] = []
    steps = 48
    for step in range(1, steps):
        y = lo + (hi - lo) * step / steps
        crossings = sorted(
            x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            for (x1, y1), (x2, y2) in edges
            if (y1 > y) != (y2 > y)
        )
        for left, right in zip(crossings[0::2], crossings[1::2], strict=False):
            chords.append((right - left, (left + right) / 2, y))

    if not chords:
        return [(sum(p[0] for p in ring) / len(ring), sum(lats) / len(lats))]

    chords.sort(reverse=True)
    spread = (hi - lo) / 6
    picked: list[tuple[float, float]] = []
    for _, x, y in chords:
        if all(abs(y - py) > spread or abs(x - px) > spread for px, py in picked):
            picked.append((x, y))
        if len(picked) == k:
            break
    return picked


# --------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: python scripts/build_taiwan_geo.py <COUNTY_MOI.gml>", file=sys.stderr)
        return 2

    import pyogrio  # type: ignore[import-untyped]

    meta, table = pyogrio.read_arrow(argv[1])
    geom_column = meta.get("geometry_name") or "wkb_geometry"
    names = table.column("名稱").to_pylist()
    codes = table.column("行政區域代碼").to_pylist()
    blobs = table.column(geom_column).to_pylist()

    counties = []
    for name, code, blob in zip(names, codes, blobs, strict=True):
        rings = []
        for ring in parse_wkb(bytes(blob)):
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            on_screen = (
                max(xs) >= MAIN[0]
                and min(xs) <= MAIN[1]
                and max(ys) >= MAIN[2]
                and min(ys) <= MAIN[3]
            )
            if not on_screen or ring_area(ring) < MIN_RING_AREA:
                continue
            thin = simplify(ring, TOLERANCE)
            if len(thin) >= 4:
                rings.append(thin)

        if not rings:
            print(f"  offshore, not drawn: {name}")
            continue

        rings.sort(key=ring_area, reverse=True)
        counties.append(
            {
                "name": name,
                "code": code,
                "rings": rings,
                "anchors": label_anchors(rings[0]),
                "area": sum(ring_area(r) for r in rings),
            }
        )

    counties.sort(key=lambda c: c["name"])
    total_points = sum(len(r) for c in counties for r in c["rings"])
    all_x = [p[0] for c in counties for r in c["rings"] for p in r]
    all_y = [p[1] for c in counties for r in c["rings"] for p in r]

    def fmt(ring: list[tuple[float, float]]) -> str:
        return "[" + ",".join(f"[{x:.4f},{y:.4f}]" for x, y in ring) + "]"

    body = ",\n".join(
        "  {\n"
        f'    name: "{c["name"]}",\n'
        f'    code: "{c["code"]}",\n'
        + "    anchors: ["
        + ",".join(f"[{x:.4f},{y:.4f}]" for x, y in c["anchors"])
        + "],\n"
        + f"    area: {c['area']:.5f},\n"
        "    rings: [\n" + ",\n".join("      " + fmt(r) for r in c["rings"]) + ",\n"
        "    ],\n"
        "  }"
        for c in counties
    )

    OUT.write_text(
        f"""/**
 * County boundaries for the main island, as [lon, lat] rings.
 *
 * Generated by `scripts/build_taiwan_geo.py` from 內政部國土測繪中心
 * 直轄市、縣市界線(TWD97經緯度) — data.gov.tw dataset 7442, edition 1140318,
 * EPSG:3824, source scale 1:5000. Released under 政府資料開放授權條款第1版,
 * which permits reuse with attribution; the map credits it in its caption.
 *
 * Simplified to {TOLERANCE}° (~165 m), which is under a third of a pixel at the
 * largest size this map is ever drawn. {len(counties)} counties, {total_points} points.
 * Do not edit by hand — re-run the generator.
 *
 * `scripts/validate_map_geometry.py` checks the result the only way that actually
 * proves anything: every monitoring station must fall inside the polygon of the
 * county its own metadata claims it is in.
 */
export interface County {{
  /** Name as it appears in the source register. */
  name: string;
  /** 行政區域代碼, stable across renames. */
  code: string;
  /** Places a label could go inside the largest ring, roomiest first. */
  anchors: [number, number][];
  /** Total area in square degrees; used to decide what gets a label. */
  area: number;
  /** Exterior rings, largest first. */
  rings: [number, number][][];
}}

export const COUNTIES: County[] = [
{body},
];

/** Bounding box of everything above: [lonMin, lonMax, latMin, latMax]. */
export const TAIWAN_BOUNDS = [
  {min(all_x):.4f}, {max(all_x):.4f}, {min(all_y):.4f}, {max(all_y):.4f},
] as const;
""",
        encoding="utf-8",
    )

    print(f"counties drawn : {len(counties)}")
    print(f"points         : {total_points}")
    print(f"bounds         : {min(all_x):.3f}–{max(all_x):.3f}E {min(all_y):.3f}–{max(all_y):.3f}N")
    print(f"written        : {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1024:.0f} kB)")

    meta_path = ROOT / "web" / "public" / "data" / "meta.json"
    stations = json.loads(meta_path.read_text(encoding="utf-8"))["stations"]
    by_name = {c["name"]: c for c in counties}
    checked = wrong = 0
    for s in stations:
        if s.get("lat") is None or s.get("county") not in by_name:
            continue
        checked += 1
        county = by_name[s["county"]]
        if not any(inside(s["lon"], s["lat"], r) for r in county["rings"]):
            wrong += 1
            print(f"  OUTSIDE {s['station_name']} claims {s['county']}")
    print(f"station check  : {checked - wrong}/{checked} inside their own county")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
