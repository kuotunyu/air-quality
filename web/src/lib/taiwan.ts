/**
 * A generalized coastline for the main island, as [lon, lat] pairs.
 *
 * Hand-encoded rather than lifted from a boundary file. A shapefile would bring
 * a dependency and a licence to track for what is, at this size, a locator
 * silhouette — so instead the outline is held to a test it can actually fail:
 * **every station the map plots must fall inside it.** Those 73 sites run the
 * length of the island and sit on both coasts, so an outline in the wrong place
 * puts a monitoring station in the sea, which is visible immediately.
 *
 * Four errors were found and fixed that way on the first run (the Lanyang
 * plain, Taitung, Linyuan and Guanyin all had the shore drawn inland of a real
 * station) and one more on the second. It currently passes for all of them.
 *
 * It is a silhouette for orientation, not a survey boundary: roughly 2 km of
 * generalization, no offshore islands, no county lines. Nothing on this site is
 * measured from it.
 */
export const TAIWAN_COAST: [number, number][] = [
  [121.53, 25.30],
  [121.65, 25.28],
  [121.86, 25.13],
  [121.95, 25.03],
  [121.92, 24.94],
  [121.83, 24.87],
  [121.84, 24.74],
  [121.86, 24.66],
  [121.83, 24.55],
  [121.86, 24.46],
  [121.72, 24.32],
  [121.66, 24.13],
  [121.62, 23.98],
  [121.58, 23.80],
  [121.50, 23.60],
  [121.42, 23.40],
  [121.38, 23.10],
  [121.27, 22.90],
  [121.21, 22.82],
  [121.18, 22.72],
  [121.06, 22.60],
  [120.95, 22.45],
  [120.90, 22.28],
  [120.87, 22.10],
  [120.85, 21.92],
  [120.75, 21.90],
  [120.72, 21.95],
  [120.70, 22.10],
  [120.62, 22.25],
  [120.59, 22.37],
  [120.51, 22.42],
  [120.42, 22.46],
  [120.36, 22.52],
  [120.28, 22.58],
  [120.25, 22.66],
  [120.20, 22.75],
  [120.15, 22.92],
  [120.10, 23.05],
  [120.10, 23.22],
  [120.13, 23.38],
  [120.10, 23.55],
  [120.13, 23.72],
  [120.20, 23.86],
  [120.30, 24.00],
  [120.38, 24.10],
  [120.48, 24.22],
  [120.55, 24.35],
  [120.65, 24.48],
  [120.78, 24.60],
  [120.85, 24.68],
  [120.92, 24.83],
  [120.98, 24.95],
  [121.04, 25.05],
  [121.16, 25.10],
  [121.28, 25.15],
  [121.41, 25.18],
  [121.50, 25.24],
];

/** Bounding box of the outline above: [lonMin, lonMax, latMin, latMax]. */
export const TAIWAN_BOUNDS = [120.10, 121.95, 21.90, 25.30] as const;
