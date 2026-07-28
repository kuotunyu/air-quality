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
