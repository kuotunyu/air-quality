/**
 * Typed access to the exported data layers.
 *
 * Everything here is read at build time by Astro from `public/data/`, which is
 * produced by `uv run twair export web`. The types mirror
 * `src/twair/viz/export.py` and `src/twair/viz/story.py`; if a field moves
 * there, the build breaks here, which is the intent.
 */

import meta from "../../public/data/meta.json";
import l0Index from "../../public/data/l0/index.json";
import trendNational from "../../public/data/story/trend-national.json";
import trendAirzone from "../../public/data/story/trend-airzone.json";
import trendCounty from "../../public/data/story/trend-county.json";
import stationCards from "../../public/data/story/station-cards.json";
import pitfalls from "../../public/data/story/pitfalls.json";
import replication from "../../public/data/story/replication.json";
import imputation from "../../public/data/story/imputation.json";
import deweather from "../../public/data/story/deweather.json";
import sarima from "../../public/data/story/sarima.json";
import { COUNTIES } from "./taiwan";

/**
 * Stations grouped by county, counties running north to south.
 *
 * A `<select>` of 74 stations sorted by name puts 二林（彰化）next to
 * 三義（苗栗）next to 土城（新北）: three counties and 300km in three rows. The
 * order a reader can actually use is geographic, and the coordinates to build it
 * are already in the register — so it is derived rather than typed out. A
 * hand-written county list would be correct today and wrong the first time the
 * register changes, on a site whose whole claim is that its numbers can be
 * checked.
 *
 * Order, in three tiers:
 *
 * 1. Main-island counties, by the mean latitude of their own stations. That is
 *    literally north to south, so the east coast interleaves with the west —
 *    宜蘭 sits between 新竹市 and 苗栗, 花蓮 between 彰化 and 南投. True to the
 *    latitude, and the alternative would mean inventing a west-then-east
 *    convention rather than reading one off the data.
 * 2. Offshore counties. "Offshore" is not a list here either: it is any county
 *    the map's own boundary data has no main-island geometry for, which is the
 *    same test `StationMap` uses to decide what it can draw. Otherwise 連江縣
 *    would lead the whole select on the strength of being at 26.15°N.
 * 3. Stations the register has no county for at all — six of them, all with
 *    decades of observations and no coordinate. Grouped and labelled rather
 *    than dropped, for the same reason the map lists them.
 */
export interface CountyGroup<T> {
  label: string;
  items: T[];
}

const MAIN_ISLAND = new Set(COUNTIES.map((county) => county.name));
const NO_COUNTY = "無縣市紀錄";

/**
 * Each county's latitude, averaged over every station the register places in
 * it — not over the stations of whichever list is being sorted.
 *
 * Computed from the list, chapter 3 and chapter 2 disagreed about 臺北市 and
 * 新北市: chapter 3 has 富貴角 at 25.30 but no 淡水, chapter 2 has 陽明 at 25.18,
 * and the two means crossed over. Same rule, different data, two different
 * orders — which is the complaint this ordering exists to answer, one level up.
 */
const countyLatitude = new Map<string, number>();
{
  const sums = new Map<string, { total: number; n: number }>();
  for (const station of meta.stations as Station[]) {
    if (station.lat == null || !station.county) continue;
    const acc = sums.get(station.county) ?? { total: 0, n: 0 };
    acc.total += station.lat;
    acc.n += 1;
    sums.set(station.county, acc);
  }
  for (const [county, { total, n }] of sums) countyLatitude.set(county, total / n);
}

export function groupByCountyNorthToSouth<T>(
  items: T[],
  nameOf: (item: T) => string,
): CountyGroup<T>[] {
  const placed = new Map(stations.map((s) => [s.station_name, s]));

  const groups = new Map<string, T[]>();
  for (const item of items) {
    const county = placed.get(nameOf(item))?.county || NO_COUNTY;
    if (!groups.has(county)) groups.set(county, []);
    groups.get(county)!.push(item);
  }

  const latitudeOf = (item: T) => placed.get(nameOf(item))?.lat ?? null;

  // Tier first, then latitude, then name — so the result is total and stable
  // rather than depending on Map insertion order for the ties.
  const tier = (county: string) =>
    county === NO_COUNTY ? 2 : MAIN_ISLAND.has(county) ? 0 : 1;

  return [...groups.entries()]
    .map(([label, group]) => ({ label, items: group, lat: countyLatitude.get(label) ?? null }))
    .sort(
      (a, b) =>
        tier(a.label) - tier(b.label) ||
        (b.lat ?? -Infinity) - (a.lat ?? -Infinity) ||
        a.label.localeCompare(b.label, "zh-Hant"),
    )
    .map(({ label, items: group }) => ({
      label,
      items: [...group].sort(
        (a, b) =>
          (latitudeOf(b) ?? -Infinity) - (latitudeOf(a) ?? -Infinity) ||
          nameOf(a).localeCompare(nameOf(b), "zh-Hant"),
      ),
    }));
}

export interface Station {
  station_name: string;
  station_name_en?: string | null;
  county?: string | null;
  township?: string | null;
  lon?: number | null;
  lat?: number | null;
  airzone: string | null;
  station_type: string;
  first_year: number;
  last_year: number;
  years_present: number;
}

export interface Pollutant {
  code: string;
  name_zh: string;
  unit: string;
  valid_range: [number, number];
  circular: boolean;
  caveat: string | null;
}

export interface StationCard {
  station_name: string;
  county?: string | null;
  township?: string | null;
  airzone?: string | null;
  station_type?: string | null;
  lon?: number | null;
  lat?: number | null;
  year: number;
  annual_mean: number;
  worst_day: number;
  days_with_data: number;
  days_over_who: number | null;
  days_over_taiwan: number | null;
  rank: number | null;
  stations_ranked: number | null;
  times_who_annual: number;
  cigarettes_per_day: number;
}

/**
 * A station-month grid for one measurand.
 *
 * `mean[s][m]` is null in two distinct cases and `n_days[s][m]` is what tells
 * them apart: 0 means the station was not measuring, greater than 0 means the
 * aggregate was withheld because coverage fell below threshold. A chart must
 * break its line in both cases and must never bridge them.
 */
export interface PollutantGrid {
  pollutant: string;
  name_zh: string;
  unit: string;
  precision: number;
  months: string[];
  stations: string[];
  mean: (number | null)[][];
  n_days: number[][];
}

export const stations = meta.stations as Station[];
export const pollutants = meta.pollutants as Pollutant[];
export const stationsWithoutCoordinates = meta.stations_without_coordinates as string[];
export const generatedAt = meta.generated_at as string;
export const gitSha = meta.git_sha as string | null;

export const pollutantIndex = l0Index.pollutants as {
  pollutant: string;
  name_zh: string;
  unit: string;
  file: string;
  months: [string, string];
  bytes: number;
}[];

export const trend = trendNational;
export const trendByAirzone = trendAirzone;
export const trendByCounty = trendCounty;
export const cards = stationCards.cards as StationCard[];
export const guidelines = stationCards.guidelines as Record<string, number | object>;
export const cigaretteCaveat = stationCards.cigarette_caveat as string;
export const pitfallTables = pitfalls.tables as Record<string, Record<string, unknown>[]>;
/** Chapter 1's second correction: how much of the fall was the weather.
 *
 * `series` carries both lines and they come from the same rows — M4's own
 * monthly output — so the difference between them cannot be the station set.
 */
export const deweatherEvidence = deweather as {
  method: string;
  series: { year: number; observed: number; normalised: number }[];
  panel: {
    balanced_since: number;
    n_stations: number;
    station_years: number;
    selection_rule: string;
    why_same_source: string;
    observed_fall: number;
    normalised_fall: number;
    weather_share_of_fall: number;
  };
  n_stations: number;
  n_significant: number;
  median_observed_slope: number;
  median_normalised_slope: number;
  median_weather_share: number;
  weather_share_p10: number;
  weather_share_p90: number;
  median_holdout_r2: number;
  caveat: string;
  by_zone: { airzone: string; n: number; observed: number; normalised: number; weather_share: number }[];
};

/**
 * Chapter 5's coda: D10 — the model the 2018 project set aside as inconvenient.
 *
 * `not_comparable` is a field rather than a comment because the chapter prints
 * it. These origins are not M9's origins, and the one thing a reader will want
 * to do with this table is set it beside the LightGBM numbers above it.
 */
export const sarimaEvidence = sarima as {
  quote: string;
  quote_page: number;
  period: number[];
  order: string;
  stations: number;
  splits: number;
  origin_stride_hours: number;
  fits: { total: number; converged: number; median_seconds: number; median_observed: number };
  selection_cost: {
    points: number;
    auto_seconds: number;
    fixed_seconds: number;
    multiple: number;
  }[];
  horizons: {
    horizon: number;
    origins: number;
    sarima_rmse: number;
    persistence_rmse: number;
    climatology_rmse: number;
    best_baseline: string;
    /** Signed so positive always means SARIMA won. */
    margin: number;
  }[];
  why_multiple_grows: string;
  why_it_loses_at_one_hour: string;
  verdict: string;
  not_comparable: string;
  no_lightgbm: string;
};

/** Pitfall 07: what the 2018 project's gap-filling sentence cost. */
export interface GapScore {
  strategy: string;
  gap_bucket: string;
  n: number;
  mae: number | null;
  rmse: number | null;
}

export const imputationEvidence = imputation as {
  period: string;
  stations_measured: number;
  stations_compared: number;
  hidden: number;
  /** Physical order, not alphabetical — ">48h" sorts first as a string. */
  buckets: string[];
  distribution: {
    gap_bucket: string;
    gaps: number;
    hours: number;
    share_of_gaps: number;
    share_of_missing_hours: number;
  }[];
  pooled: {
    strategy: string;
    recovered: number;
    recovery_rate: number | null;
    mae: number | null;
    rmse: number | null;
    bias: number | null;
  }[];
  by_gap: GapScore[];
  method: Record<string, unknown>;
  not_reported: Record<string, string>;
};

export const replicationRows = replication.rows as {
  kind: string;
  item: string;
  published_2018: number | null;
  reproduced: number | null;
  difference: number | null;
  pct_difference: number | null;
}[];

/** Stations that can be drawn on a map, sorted north to south. */
export function mappableStations(): Station[] {
  return stations
    .filter((s) => s.lat != null && s.lon != null)
    .sort((a, b) => (b.lat as number) - (a.lat as number));
}

/** Prefix a public asset path with the configured base, without doubling slashes. */
export function asset(path: string): string {
  const base = import.meta.env.BASE_URL ?? "/";
  return `${base.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
}
