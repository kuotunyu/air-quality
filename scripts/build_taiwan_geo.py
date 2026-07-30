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

Ring = list[tuple[float, float]]

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
def parse_wkb(blob: bytes) -> list[tuple[Ring, list[Ring]]]:
    """Return each polygon as ``(exterior ring, [interior rings])``.

    Interior rings are kept, and that is not a formality on this dataset. An
    earlier version dropped them on the reasoning that "an enclave would be a
    separate feature, and a hole at this scale would be smaller than a pixel".
    Both halves are false here, and they are false in a way that is invisible in
    the output: 臺北市 is a separate feature *and* a hole in 新北市, because a
    landlocked enclave cannot be expressed by an exterior ring alone. The same
    goes for 嘉義市 inside 嘉義縣. The source has exactly two interior rings and
    those are they — 0.0242 and 0.0053 square degrees, 240x and 53x the
    threshold below, and about 39x51 px on the map rather than sub-pixel.

    Dropping them shipped two counties whose polygons *overlap* their
    neighbours: Taipei City Hall tested inside both 臺北市 and 新北市. It also
    inflated 新北市's area by 13%, which is the sort key the label placement
    uses, and left 嘉義市 painted over completely by 嘉義縣's opaque fill.
    """
    polygons: list[tuple[Ring, list[Ring]]] = []
    pos = 0

    def take(fmt: str) -> tuple[Any, ...]:
        nonlocal pos
        size = struct.calcsize(fmt)
        out = struct.unpack_from(fmt, blob, pos)
        pos += size
        return out

    def read_polygon(endian: str) -> None:
        (n_rings,) = take(endian + "I")
        exterior: Ring = []
        interiors: list[Ring] = []
        for index in range(int(n_rings)):
            (n_points,) = take(endian + "I")
            coords = take(endian + f"{int(n_points) * 2}d")
            ring = list(zip(coords[0::2], coords[1::2], strict=True))
            if index == 0:
                exterior = ring
            else:
                interiors.append(ring)
        polygons.append((exterior, interiors))

    (byte_order,) = take("B")
    endian = "<" if byte_order == 1 else ">"
    (geom_type,) = take(endian + "I")

    # ISO WKB encodes dimensionality in the thousands digit: 1000 adds Z, 2000
    # adds M, 3000 adds both. Every point then carries 3 or 4 doubles instead of
    # 2. Matching on `% 1000` alone accepted all of them and went on reading
    # pairs, which does not raise — it silently returns coordinates assembled
    # from interleaved elevations. This dataset is 2D, and if that ever changes
    # the run should stop rather than draw a plausible wrong island.
    if geom_type >= 1000:
        raise ValueError(f"WKB type {geom_type} carries Z and/or M values; this reader is 2D only")

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

    return polygons


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


def inside(px: float, py: float, rings: list[Ring]) -> bool:
    """Even-odd containment across *all* of a county's rings at once.

    Accumulating the crossings over every ring rather than asking each one
    separately is what makes a hole behave like a hole: a point in 臺北市
    crosses 新北市's exterior once and its interior ring once, and comes out
    even, which is "outside". Testing ring by ring and OR-ing the answers —
    which is what the first version of the validator did — reports such a point
    as inside both counties and can never notice the overlap.

    This is also exactly the rule the map paints with (`fill-rule: evenodd`), so
    the check and the drawing cannot disagree.
    """
    hit = False
    for ring in rings:
        for i in range(len(ring)):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % len(ring)]
            straddles = (y1 > py) != (y2 > py)
            if straddles and px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
                hit = not hit
    return hit


def label_anchors(ring: Ring, holes: list[Ring], k: int = 4) -> list[tuple[float, float]]:
    """Several places a name could go, roomiest first.

    One anchor is not enough. The centroid is no good to begin with — Chiayi
    wraps around Chiayi City and Taitung is a crescent, so their centroids fall
    in the sea — and even the widest interior chord fails when two neighbours
    are both widest along the same edge: Kaohsiung and Pingtung are adjacent and
    both open out onto the same southern plain, so whichever is placed first
    takes the space and the other loses its label entirely.

    Offering the caller a short list of well-separated candidates lets it fall
    back to the county's second-roomiest spot instead of giving up.

    ``holes`` matters for exactly two counties and would be invisible without
    it. The chord scan runs on the exterior ring, so for 新北市 the roomiest
    interval it finds runs straight through 臺北市 — the third candidate the
    earlier version emitted, [121.4648, 25.1172], is in 北投區. A label there
    names the wrong county.
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
        if holes and any(inside(x, y, [hole]) for hole in holes):
            continue
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

    def keep(ring: Ring) -> Ring | None:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        on_screen = (
            max(xs) >= MAIN[0] and min(xs) <= MAIN[1] and max(ys) >= MAIN[2] and min(ys) <= MAIN[3]
        )
        if not on_screen or ring_area(ring) < MIN_RING_AREA:
            return None
        thin = simplify(ring, TOLERANCE)
        return thin if len(thin) >= 4 else None

    counties = []
    for name, code, blob in zip(names, codes, blobs, strict=True):
        outer: list[Ring] = []
        holes: list[Ring] = []
        for exterior, interiors in parse_wkb(bytes(blob)):
            thin = keep(exterior)
            if thin is None:
                continue
            outer.append(thin)
            holes.extend(h for h in (keep(i) for i in interiors) if h is not None)

        if not outer:
            # The map's caption names these, but it derives them from the
            # stations that fall outside the frame rather than from a list
            # emitted here — same fact, one fewer thing to keep in sync.
            print(f"  offshore, not drawn: {name}")
            continue

        outer.sort(key=ring_area, reverse=True)
        # Area is what the label placement sorts on, so it has to be the land
        # the county actually governs. Summing exteriors alone credited 新北市
        # with all of 臺北市 — 13% too much — and moved it three places up the
        # queue.
        area = sum(ring_area(r) for r in outer) - sum(ring_area(r) for r in holes)
        if holes:
            print(f"  {name}: {len(holes)} enclave(s), area {area:.5f} after subtracting them")
        counties.append(
            {
                "name": name,
                "code": code,
                # Exteriors and holes together, to be filled `evenodd`.
                "rings": outer + holes,
                "outer": outer,
                "holes": holes,
                "anchors": label_anchors(outer[0], holes),
                "area": area,
            }
        )

    counties.sort(key=lambda c: c["name"])

    # A label must not sit on a neighbour. The per-county hole test above cannot
    # catch this on its own: an anchor can be genuinely inside its own county
    # and still land where a *different* county is drawn on top, which is how
    # the enclaves were invisible in the first place.
    for county in counties:
        others = [o for o in counties if o["name"] != county["name"]]
        county["anchors"] = [
            (x, y)
            for x, y in county["anchors"]
            if not any(inside(x, y, o["rings"]) for o in others)
        ] or county["anchors"][:1]
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
 * `scripts/validate_map_geometry.py` checks the result the only way that
 * actually proves anything: every monitoring station must fall inside the
 * polygon of the county its own metadata claims — and inside no other.
 */
export interface County {{
  /** Name as it appears in the source register. */
  name: string;
  /** 行政區域代碼, stable across renames. */
  code: string;
  /** Places a label could go, roomiest first; none inside a hole or a neighbour. */
  anchors: [number, number][];
  /** Square degrees, holes subtracted. The label placement sorts on it. */
  area: number;
  /**
   * Exterior rings and holes together, largest exterior first.
   *
   * **Must be filled `evenodd`.** Two counties are true enclaves — 臺北市 in
   * 新北市, 嘉義市 in 嘉義縣 — so the surrounding county's ring list contains a
   * hole where the enclave sits. Filled `nonzero` the hole would depend on
   * winding direction and the enclave would be painted over; there is no
   * exterior-ring-only way to express a landlocked county.
   */
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
    checked = wrong = ambiguous = 0
    for s in stations:
        if s.get("lat") is None or s.get("county") not in by_name:
            continue
        checked += 1
        holding = [c["name"] for c in counties if inside(s["lon"], s["lat"], c["rings"])]
        if s["county"] not in holding:
            wrong += 1
            print(f"  OUTSIDE  {s['station_name']} claims {s['county']}, found in {holding}")
        elif len(holding) > 1:
            ambiguous += 1
            print(f"  OVERLAP  {s['station_name']} is inside {holding}")
    print(f"station check  : {checked - wrong - ambiguous}/{checked} in exactly their own county")
    return 1 if (wrong or ambiguous) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
