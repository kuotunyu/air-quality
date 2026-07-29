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
