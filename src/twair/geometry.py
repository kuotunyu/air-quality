"""One definition of distance on the earth's surface.

There were two. `qc/gapfill.py` carried a private great-circle helper for
「借鄰站的值」, and M6 needs the same metric in four more places: correlogram bin
edges, the longest-link diagnostic, the blocked cross-validation radii, and the
`radius=` argument libpysal wants before it will stop measuring in degrees.

A second copy is not a duplication problem, it is a *drift* problem. If one of
them uses 6371.0 and the other 6371.0088, a station sits in the 20–30 km
correlogram bin according to one module and the 30–50 km bin according to the
other, and nothing fails — the two numbers are simply about slightly different
worlds. So the radius is a named constant here and nowhere else.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["EARTH_RADIUS_KM", "haversine_km", "pairwise_km"]

# Mean earth radius, IUGG. Taiwan is small enough that the ellipsoid does not
# matter; what matters is that every caller uses the same number.
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def pairwise_km(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Full symmetric distance matrix for a set of points, in kilometres.

    Vectorised because the caller needs all n² distances at once — a correlogram
    bins every pair, and a weights matrix is defined by all of them. The
    diagonal is exactly zero rather than a rounding artefact of `arcsin`.
    """
    p = np.radians(np.asarray(lat, dtype=float))
    l = np.radians(np.asarray(lon, dtype=float))  # noqa: E741
    dp = p[:, None] - p[None, :]
    dl = l[:, None] - l[None, :]
    a = np.sin(dp / 2) ** 2 + np.cos(p)[:, None] * np.cos(p)[None, :] * np.sin(dl / 2) ** 2
    # Clip before arcsin: floating point can push a self-distance to 1+1e-17.
    out: np.ndarray = 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    np.fill_diagonal(out, 0.0)
    return out
