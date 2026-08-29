"""Frozen scientific contract for the four-receptor HYSPLIT pilot."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import polars as pl

from twair.analysis.sources import CALM_THRESHOLD_MS, DEFAULT_SPEED_BINS, build_wind_frame
from twair.config import ConfigError, load_conf
from twair.ingest.station_meta import TAIWAN_BOUNDS, load_station_geo
from twair.paths import outputs_dir

_RECEPTORS = ("富貴角", "麥寮", "楠梓", "花蓮")
_SPEED_BINS_MS = (0.5, 1.5, 2.5, 4.0, 6.0, 8.0)
_START_HEIGHTS_M_AGL = (100, 300, 500)
_MATCHING = {
    "same_month": True,
    "same_local_hour": True,
    "same_direction_sector": True,
    "same_speed_bin": True,
    "without_replacement": True,
    "allow_relaxation": False,
}
_OFFICIAL_SOURCES = {
    "download": "https://www.ready.noaa.gov/HYSPLIT_hytrial.php",
    "control_format": "https://www.ready.noaa.gov/hysplitusersguide/S262.htm",
    "endpoint_format": "https://www.ready.noaa.gov/hysplitusersguide/S263.htm",
    "gdas1": "https://www.ready.noaa.gov/gdas1.php",
}
_CLAIM_BOUNDARY = {
    "pathway_only": True,
    "source_identity": False,
    "source_location": False,
    "source_contribution": False,
    "causal_attribution": False,
    "concentration_field": False,
}
_EXPECTED_ANALYSIS: dict[str, object] = {
    "year": 2025,
    "pollutant": "PM2.5",
    "receptors": list(_RECEPTORS),
    "event_percentile": 90.0,
    "control_percentile": 50.0,
    "max_events_per_station": 30,
    "event_separation_hours": 72,
    "calm_threshold_ms": 0.5,
    "direction_sector_degrees": 30,
    "speed_bins_ms": list(_SPEED_BINS_MS),
    "matching": _MATCHING,
    "duration_hours": -72,
    "start_heights_m_agl": list(_START_HEIGHTS_M_AGL),
    "vertical_motion": 0,
    "model_top_m_agl": 10000.0,
    "meteorology_dataset": "gdas1",
    "official_sources": _OFFICIAL_SOURCES,
    "claim_boundary": _CLAIM_BOUNDARY,
}
_FIELD_LABELS = {
    "receptors": "receptors",
    "speed_bins_ms": "speed bins",
    "duration_hours": "duration",
    "vertical_motion": "vertical motion",
    "claim_boundary": "claim boundary",
}
_REQUIRED_WIND_COLUMNS = ("station_name", "ts_local", "PM2.5", "WS_HR", "WD_HR")
_REQUIRED_GEO_COLUMNS = ("station_name", "station_name_en", "lon", "lat")
_ARRIVALS_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "pair_id": pl.String,
    "arrival_kind": pl.String,
    "match_state": pl.String,
    "station_name": pl.String,
    "station_name_en": pl.String,
    "latitude": pl.Float64,
    "longitude": pl.Float64,
    "event_rank": pl.Int64,
    "ts_local": pl.Datetime("us"),
    "arrival_utc": pl.Datetime("us", "UTC"),
    "pm25": pl.Float64,
    "ws_hr": pl.Float64,
    "wd_hr": pl.Float64,
    "direction_sector": pl.Int64,
    "speed_bin": pl.String,
    "station_p90": pl.Float64,
    "station_median": pl.Float64,
}
_RUNS_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "run_id": pl.String,
    "pair_id": pl.String,
    "arrival_kind": pl.String,
    "station_name": pl.String,
    "arrival_utc": pl.Datetime("us", "UTC"),
    "latitude": pl.Float64,
    "longitude": pl.Float64,
    "start_height_m_agl": pl.Int64,
    "duration_hours": pl.Int64,
    "vertical_motion": pl.Int64,
    "model_top_m_agl": pl.Float64,
    "meteorology_dataset": pl.String,
}
_GENERATION_MEMBERS = ("arrivals.parquet", "runs.parquet", "summary.json", "manifest.json")


@dataclass(frozen=True, slots=True)
class HysplitProtocol:
    """Reviewed constants that define the pilot and its public claim boundary."""

    year: int
    pollutant: str
    receptors: tuple[str, ...]
    event_percentile: float
    control_percentile: float
    max_events_per_station: int
    event_separation_hours: int
    calm_threshold_ms: float
    direction_sector_degrees: int
    speed_bins_ms: tuple[float, ...]
    matching: Mapping[str, bool]
    duration_hours: int
    start_heights_m_agl: tuple[int, ...]
    vertical_motion: int
    model_top_m_agl: float
    meteorology_dataset: str
    official_sources: Mapping[str, str]
    claim_boundary: Mapping[str, bool]


@dataclass(frozen=True, slots=True)
class PilotPlan:
    """Deterministic event/control arrivals and their standard trajectory matrix."""

    arrivals: pl.DataFrame
    runs: pl.DataFrame
    summary: dict[str, int]
    input_identities: dict[str, object]


def _same_typed_value(actual: object, expected: object) -> bool:
    """Compare nested config values without treating ``True`` as integer ``1``."""
    if isinstance(expected, dict):
        if not isinstance(actual, Mapping) or set(actual) != set(expected):
            return False
        return all(
            _same_typed_value(actual[key], expected_value)
            for key, expected_value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(
            _same_typed_value(actual_value, expected_value)
            for actual_value, expected_value in zip(actual, expected, strict=True)
        )
    return type(actual) is type(expected) and actual == expected


def load_hysplit_protocol(
    config: Mapping[str, object] | None = None,
) -> HysplitProtocol:
    """Load the reviewed protocol, rejecting any silent experimental drift."""
    raw: Mapping[str, object] = load_conf("hysplit_pilot") if config is None else config
    if not _same_typed_value(raw.get("schema_version"), 1):
        raise ConfigError("HYSPLIT protocol schema version must be exactly 1")

    analysis = raw.get("analysis")
    if not isinstance(analysis, Mapping):
        raise ConfigError("HYSPLIT protocol analysis must be a mapping")

    matching = analysis.get("matching")
    if isinstance(matching, Mapping) and matching.get("allow_relaxation") is True:
        raise ConfigError("HYSPLIT matching relaxation is forbidden")

    if set(analysis) != set(_EXPECTED_ANALYSIS):
        raise ConfigError("HYSPLIT analysis fields differ from the reviewed protocol")
    for field, expected in _EXPECTED_ANALYSIS.items():
        if not _same_typed_value(analysis.get(field), expected):
            label = _FIELD_LABELS.get(field, field.replace("_", " "))
            raise ConfigError(f"HYSPLIT {label} differs from the reviewed protocol")

    return HysplitProtocol(
        year=2025,
        pollutant="PM2.5",
        receptors=_RECEPTORS,
        event_percentile=90.0,
        control_percentile=50.0,
        max_events_per_station=30,
        event_separation_hours=72,
        calm_threshold_ms=0.5,
        direction_sector_degrees=30,
        speed_bins_ms=_SPEED_BINS_MS,
        matching=MappingProxyType(dict(_MATCHING)),
        duration_hours=-72,
        start_heights_m_agl=_START_HEIGHTS_M_AGL,
        vertical_motion=0,
        model_top_m_agl=10000.0,
        meteorology_dataset="gdas1",
        official_sources=MappingProxyType(dict(_OFFICIAL_SOURCES)),
        claim_boundary=MappingProxyType(dict(_CLAIM_BOUNDARY)),
    )


def validate_ascii_external_path(path: Path, *, label: str) -> Path:
    """Validate a future external-tool path without touching the filesystem."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError(f"{label} must be absolute")
    rendered = str(expanded)
    if any(ord(character) >= 128 or character.isspace() for character in rendered):
        raise ValueError(f"{label} must be ASCII-only and contain no whitespace")
    return expanded.absolute()


def _require_columns(frame: pl.DataFrame, required: tuple[str, ...], *, label: str) -> None:
    missing = set(required) - set(frame.columns)
    if missing:
        raise ConfigError(f"{label} is missing columns {sorted(missing)}")


def _validated_geography(
    station_geo: pl.DataFrame,
    protocol: HysplitProtocol,
) -> dict[str, dict[str, object]]:
    _require_columns(station_geo, _REQUIRED_GEO_COLUMNS, label="station geography")
    selected = (
        station_geo.filter(pl.col("station_name").is_in(protocol.receptors))
        .select(_REQUIRED_GEO_COLUMNS)
        .with_columns(
            pl.col("lon").cast(pl.Float64, strict=False),
            pl.col("lat").cast(pl.Float64, strict=False),
        )
    )
    counts = selected.group_by("station_name").len()
    if (
        selected.height != len(protocol.receptors)
        or counts.height != len(protocol.receptors)
        or counts.filter(pl.col("len") != 1).height
    ):
        raise ConfigError("HYSPLIT pilot requires exactly one geography row per receptor")

    invalid_english = selected.filter(
        pl.col("station_name_en").is_null() | (pl.col("station_name_en").str.strip_chars() == "")
    )
    if not invalid_english.is_empty():
        raise ConfigError("HYSPLIT receptor English name must be present")

    non_finite = selected.filter(
        pl.col("lon").is_null()
        | pl.col("lat").is_null()
        | ~pl.col("lon").is_finite()
        | ~pl.col("lat").is_finite()
    )
    if not non_finite.is_empty():
        raise ConfigError("HYSPLIT receptor geography requires finite coordinates")

    outside = selected.filter(
        ~pl.col("lon").is_between(TAIWAN_BOUNDS["lon_min"], TAIWAN_BOUNDS["lon_max"])
        | ~pl.col("lat").is_between(TAIWAN_BOUNDS["lat_min"], TAIWAN_BOUNDS["lat_max"])
    )
    if not outside.is_empty():
        raise ConfigError("HYSPLIT receptor coordinate is outside Taiwan")

    return {str(row["station_name"]): row for row in selected.iter_rows(named=True)}


def _speed_bin(value: float, bins: tuple[float, ...]) -> str:
    if value <= bins[0]:
        return f"<{bins[0]:g}"
    for lower, upper in pairwise(bins):
        if value <= upper:
            return f"{lower:g}-{upper:g}"
    return f"{bins[-1]:g}+"


def _direction_sector(value: float, width: int) -> int:
    return int((value % 360.0) // width * width)


def _utc_from_local(value: datetime) -> datetime:
    if value.tzinfo is not None:
        raise ConfigError("HYSPLIT source ts_local values must be timezone-naive")
    return value.replace(tzinfo=ZoneInfo("Asia/Taipei")).astimezone(UTC)


def _ascii_slug(value: str) -> str:
    if any(ord(character) >= 128 for character in value):
        raise ConfigError("HYSPLIT receptor English name must be ASCII")
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise ConfigError("HYSPLIT receptor English name cannot form a run slug")
    return slug


def _float_value(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be numeric")
    return float(value)


def _canonical_scalar(value: object) -> object:
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeError("HYSPLIT generation cannot contain non-finite values")
        return value
    if isinstance(value, tuple):
        return [_canonical_scalar(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_value(value: object) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _frame_identity(frame: pl.DataFrame) -> dict[str, object]:
    payload = {
        "schema": [[name, str(dtype)] for name, dtype in frame.schema.items()],
        "rows": [[_canonical_scalar(value) for value in row] for row in frame.iter_rows()],
    }
    return {
        "rows": frame.height,
        "sha256": _hash_value(payload),
        "schema": {name: str(dtype) for name, dtype in frame.schema.items()},
    }


def _arrival_row(
    source: Mapping[str, Any],
    *,
    pair_id: str,
    arrival_kind: str,
    match_state: str,
    event_rank: int,
    geography: Mapping[str, object],
    station_p90: float,
    station_median: float,
    protocol: HysplitProtocol,
) -> dict[str, object]:
    local = source["ts_local"]
    if not isinstance(local, datetime):
        raise ConfigError("HYSPLIT source ts_local values must be datetimes")
    ws_hr = float(source["WS_HR"])
    wd_hr = float(source["WD_HR"])
    return {
        "pair_id": pair_id,
        "arrival_kind": arrival_kind,
        "match_state": match_state,
        "station_name": str(source["station_name"]),
        "station_name_en": str(geography["station_name_en"]),
        "latitude": _float_value(geography["lat"], label="HYSPLIT receptor latitude"),
        "longitude": _float_value(geography["lon"], label="HYSPLIT receptor longitude"),
        "event_rank": event_rank,
        "ts_local": local,
        "arrival_utc": _utc_from_local(local),
        "pm25": float(source["PM2.5"]),
        "ws_hr": ws_hr,
        "wd_hr": wd_hr,
        "direction_sector": _direction_sector(wd_hr, protocol.direction_sector_degrees),
        "speed_bin": _speed_bin(ws_hr, protocol.speed_bins_ms),
        "station_p90": station_p90,
        "station_median": station_median,
    }


def _matching_control(
    event: Mapping[str, Any],
    controls: list[dict[str, Any]],
    used: set[datetime],
    protocol: HysplitProtocol,
) -> dict[str, Any] | None:
    event_time = event["ts_local"]
    if not isinstance(event_time, datetime):
        raise ConfigError("HYSPLIT source ts_local values must be datetimes")
    event_sector = _direction_sector(float(event["WD_HR"]), protocol.direction_sector_degrees)
    event_speed = _speed_bin(float(event["WS_HR"]), protocol.speed_bins_ms)
    eligible: list[dict[str, Any]] = []
    for control in controls:
        control_time = control["ts_local"]
        if not isinstance(control_time, datetime) or control_time in used:
            continue
        if (
            control_time.month == event_time.month
            and control_time.hour == event_time.hour
            and _direction_sector(float(control["WD_HR"]), protocol.direction_sector_degrees)
            == event_sector
            and _speed_bin(float(control["WS_HR"]), protocol.speed_bins_ms) == event_speed
        ):
            eligible.append(control)
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda row: (
            abs((row["ts_local"] - event_time).total_seconds()),
            row["ts_local"],
        ),
    )


def build_hysplit_pilot_plan(
    frame: pl.DataFrame,
    station_geo: pl.DataFrame,
    protocol: HysplitProtocol,
) -> PilotPlan:
    """Select reviewed events and controls and expand matched pairs into runs."""
    if protocol.speed_bins_ms != DEFAULT_SPEED_BINS:
        raise ConfigError("HYSPLIT speed bins disagree with the existing M7 contract")
    if protocol.calm_threshold_ms != CALM_THRESHOLD_MS:
        raise ConfigError("HYSPLIT calm threshold disagrees with the existing wind contract")
    _require_columns(frame, _REQUIRED_WIND_COLUMNS, label="HYSPLIT wind frame")
    geography = _validated_geography(station_geo, protocol)

    usable = (
        frame.select(_REQUIRED_WIND_COLUMNS)
        .filter(pl.col("station_name").is_in(protocol.receptors))
        .with_columns(
            pl.col("PM2.5").cast(pl.Float64, strict=False),
            pl.col("WS_HR").cast(pl.Float64, strict=False),
            pl.col("WD_HR").cast(pl.Float64, strict=False),
        )
        .drop_nulls(_REQUIRED_WIND_COLUMNS)
        .filter(
            pl.col("PM2.5").is_finite() & pl.col("WS_HR").is_finite() & pl.col("WD_HR").is_finite()
        )
    )
    duplicates = usable.group_by("station_name", "ts_local").len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ConfigError("HYSPLIT wind frame has duplicate station timestamps")

    arrival_rows: list[dict[str, object]] = []
    run_rows: list[dict[str, object]] = []
    matched_pairs = 0
    selected_events = 0

    for station in protocol.receptors:
        station_frame = usable.filter(pl.col("station_name") == station)
        if station_frame.is_empty():
            continue
        p90_value = station_frame[protocol.pollutant].quantile(
            protocol.event_percentile / 100.0,
            interpolation="linear",
        )
        median_value = station_frame[protocol.pollutant].quantile(
            protocol.control_percentile / 100.0,
            interpolation="linear",
        )
        if p90_value is None or median_value is None:
            raise ConfigError(f"HYSPLIT thresholds are unavailable for {station}")
        station_p90 = float(p90_value)
        station_median = float(median_value)

        candidates = (
            station_frame.filter(
                (pl.col(protocol.pollutant) >= station_p90)
                & (pl.col("WS_HR") > protocol.calm_threshold_ms)
            )
            .sort([protocol.pollutant, "ts_local"], descending=[True, False])
            .to_dicts()
        )
        retained: list[dict[str, Any]] = []
        separation = timedelta(hours=protocol.event_separation_hours)
        for candidate in candidates:
            timestamp = candidate["ts_local"]
            if not isinstance(timestamp, datetime):
                raise ConfigError("HYSPLIT source ts_local values must be datetimes")
            if all(abs(timestamp - earlier["ts_local"]) >= separation for earlier in retained):
                retained.append(candidate)
                if len(retained) == protocol.max_events_per_station:
                    break

        controls = station_frame.filter(pl.col(protocol.pollutant) <= station_median).to_dicts()
        used_controls: set[datetime] = set()
        geo = geography[station]
        station_slug = _ascii_slug(str(geo["station_name_en"]))
        for rank, event in enumerate(retained, start=1):
            event_time = event["ts_local"]
            if not isinstance(event_time, datetime):
                raise ConfigError("HYSPLIT source ts_local values must be datetimes")
            event_utc = _utc_from_local(event_time)
            pair_id = f"{station_slug}-{event_utc:%Y%m%d%H%M}-e{rank:02d}"
            control = _matching_control(event, controls, used_controls, protocol)
            match_state = "matched" if control is not None else "unmatched_no_control"
            event_arrival = _arrival_row(
                event,
                pair_id=pair_id,
                arrival_kind="event",
                match_state=match_state,
                event_rank=rank,
                geography=geo,
                station_p90=station_p90,
                station_median=station_median,
                protocol=protocol,
            )
            arrival_rows.append(event_arrival)
            selected_events += 1
            if control is None:
                continue

            control_time = control["ts_local"]
            if not isinstance(control_time, datetime):
                raise ConfigError("HYSPLIT source ts_local values must be datetimes")
            used_controls.add(control_time)
            control_arrival = _arrival_row(
                control,
                pair_id=pair_id,
                arrival_kind="control",
                match_state="matched",
                event_rank=rank,
                geography=geo,
                station_p90=station_p90,
                station_median=station_median,
                protocol=protocol,
            )
            arrival_rows.append(control_arrival)
            matched_pairs += 1
            for arrival in (event_arrival, control_arrival):
                arrival_utc = arrival["arrival_utc"]
                if not isinstance(arrival_utc, datetime):
                    raise ConfigError("HYSPLIT arrival UTC must be a datetime")
                for height in protocol.start_heights_m_agl:
                    run_rows.append(
                        {
                            "run_id": (
                                f"{station_slug}-{arrival_utc:%Y%m%d%H%M}-"
                                f"{arrival['arrival_kind']}-{height}m"
                            ),
                            "pair_id": pair_id,
                            "arrival_kind": arrival["arrival_kind"],
                            "station_name": station,
                            "arrival_utc": arrival_utc,
                            "latitude": arrival["latitude"],
                            "longitude": arrival["longitude"],
                            "start_height_m_agl": height,
                            "duration_hours": protocol.duration_hours,
                            "vertical_motion": protocol.vertical_motion,
                            "model_top_m_agl": protocol.model_top_m_agl,
                            "meteorology_dataset": protocol.meteorology_dataset,
                        }
                    )

    arrivals = pl.DataFrame(arrival_rows, schema=_ARRIVALS_SCHEMA)
    runs = pl.DataFrame(run_rows, schema=_RUNS_SCHEMA)
    summary = {
        "selected_events": selected_events,
        "matched_pairs": matched_pairs,
        "unmatched_events": selected_events - matched_pairs,
        "standard_runs": runs.height,
    }
    geography_frame = pl.DataFrame([geography[station] for station in protocol.receptors]).select(
        _REQUIRED_GEO_COLUMNS
    )
    input_identities: dict[str, object] = {
        "wind_frame": _frame_identity(usable.sort("station_name", "ts_local")),
        "station_geography": _frame_identity(geography_frame),
    }
    return PilotPlan(
        arrivals=arrivals,
        runs=runs,
        summary=summary,
        input_identities=input_identities,
    )


def prepare_hysplit_pilot_plan(root: Path | None = None) -> PilotPlan:
    """Prepare the full 2025 plan from local reviewed data, without external I/O."""
    protocol = load_hysplit_protocol()
    frame = build_wind_frame(
        root,
        period=(protocol.year, protocol.year),
        pollutant=protocol.pollutant,
        stations=list(protocol.receptors),
    )
    available = set(frame.get_column("station_name").unique().to_list())
    missing = set(protocol.receptors) - available
    if missing:
        raise ConfigError(f"HYSPLIT wind frame is missing receptor data: {sorted(missing)}")
    return build_hysplit_pilot_plan(frame, load_station_geo(), protocol)


def _protocol_document(protocol: HysplitProtocol) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "analysis": {
            "year": protocol.year,
            "pollutant": protocol.pollutant,
            "receptors": list(protocol.receptors),
            "event_percentile": protocol.event_percentile,
            "control_percentile": protocol.control_percentile,
            "max_events_per_station": protocol.max_events_per_station,
            "event_separation_hours": protocol.event_separation_hours,
            "calm_threshold_ms": protocol.calm_threshold_ms,
            "direction_sector_degrees": protocol.direction_sector_degrees,
            "speed_bins_ms": list(protocol.speed_bins_ms),
            "matching": dict(protocol.matching),
            "duration_hours": protocol.duration_hours,
            "start_heights_m_agl": list(protocol.start_heights_m_agl),
            "vertical_motion": protocol.vertical_motion,
            "model_top_m_agl": protocol.model_top_m_agl,
            "meteorology_dataset": protocol.meteorology_dataset,
            "official_sources": dict(protocol.official_sources),
            "claim_boundary": dict(protocol.claim_boundary),
        },
    }
    try:
        load_hysplit_protocol(document)
    except ConfigError as exc:
        raise RuntimeError("HYSPLIT plan protocol differs from the reviewed contract") from exc
    return document


def _normalize_plan_table(name: str, frame: pl.DataFrame) -> pl.DataFrame:
    schemas = {"arrivals": _ARRIVALS_SCHEMA, "runs": _RUNS_SCHEMA}
    schema = schemas[name]
    if frame.columns != list(schema):
        raise RuntimeError(f"HYSPLIT {name} columns differ from the reviewed schema")
    try:
        return frame.cast(pl.Schema(schema), strict=True)
    except (TypeError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"HYSPLIT {name} types differ from the reviewed schema") from exc


def _expected_plan_summary(arrivals: pl.DataFrame, runs: pl.DataFrame) -> dict[str, int]:
    events = arrivals.filter(pl.col("arrival_kind") == "event")
    controls = arrivals.filter(pl.col("arrival_kind") == "control")
    matched = events.filter(pl.col("match_state") == "matched")
    unmatched = events.filter(pl.col("match_state") == "unmatched_no_control")
    if events.height + controls.height != arrivals.height:
        raise RuntimeError("HYSPLIT arrivals contain an unknown arrival kind")
    if matched.height + unmatched.height != events.height:
        raise RuntimeError("HYSPLIT event match state differs from the reviewed contract")
    if (
        controls.height != matched.height
        or controls.filter(pl.col("match_state") != "matched").height
    ):
        raise RuntimeError("HYSPLIT controls do not form one-to-one matched pairs")
    pair_counts = arrivals.group_by("pair_id").agg(
        pl.len().alias("rows"),
        pl.col("arrival_kind").n_unique().alias("kinds"),
    )
    if pair_counts.filter(
        ~(
            ((pl.col("rows") == 2) & (pl.col("kinds") == 2))
            | ((pl.col("rows") == 1) & (pl.col("kinds") == 1))
        )
    ).height:
        raise RuntimeError("HYSPLIT pair membership differs from the reviewed contract")
    if runs["run_id"].n_unique() != runs.height:
        raise RuntimeError("HYSPLIT run IDs are not unique")
    expected_runs = matched.height * 2 * len(_START_HEIGHTS_M_AGL)
    if runs.height != expected_runs:
        raise RuntimeError("HYSPLIT run matrix is incomplete")
    return {
        "selected_events": events.height,
        "matched_pairs": matched.height,
        "unmatched_events": unmatched.height,
        "standard_runs": runs.height,
    }


def _plan_identity(
    plan: PilotPlan,
    *,
    members: dict[str, dict[str, object]],
) -> dict[str, object]:
    protocol = load_hysplit_protocol()
    arrivals = _normalize_plan_table("arrivals", plan.arrivals)
    runs = _normalize_plan_table("runs", plan.runs)
    expected_summary = _expected_plan_summary(arrivals, runs)
    if plan.summary != expected_summary:
        raise RuntimeError("HYSPLIT plan summary differs from its tables")
    if set(plan.input_identities) != {"wind_frame", "station_geography"}:
        raise RuntimeError("HYSPLIT plan input identities are incomplete")
    return {
        "schema_version": 1,
        "analysis": "hysplit_pilot_plan",
        "protocol": _protocol_document(protocol),
        "ordered_receptors": list(protocol.receptors),
        "claim_boundary": dict(protocol.claim_boundary),
        "input_identities": plan.input_identities,
        "tables": {
            "arrivals": _frame_identity(arrivals),
            "runs": _frame_identity(runs),
        },
        "summary": expected_summary,
        "members": members,
    }


def _file_identity(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {"bytes": len(payload), "sha256": sha256(payload).hexdigest()}


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _written_plan_paths(directory: Path) -> dict[str, Path]:
    return {
        "arrivals": directory / "arrivals.parquet",
        "runs": directory / "runs.parquet",
        "summary": directory / "summary.json",
        "manifest": directory / "manifest.json",
    }


def _load_hysplit_pilot_plan(
    directory: Path,
    *,
    require_directory_name: bool,
) -> PilotPlan:
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError("HYSPLIT generation is missing or linked")
    entries = tuple(directory.iterdir())
    if {entry.name for entry in entries} != set(_GENERATION_MEMBERS):
        raise RuntimeError("HYSPLIT generation has an unexpected member inventory")
    if any(not entry.is_file() or entry.is_symlink() for entry in entries):
        raise RuntimeError("HYSPLIT generation contains a non-regular member")

    manifest_payload = (directory / "manifest.json").read_bytes()
    try:
        manifest = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("HYSPLIT manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest_payload != _canonical_json_bytes(manifest):
        raise RuntimeError("HYSPLIT manifest is not canonical JSON")
    expected_manifest_fields = {
        "schema_version",
        "analysis",
        "protocol",
        "ordered_receptors",
        "claim_boundary",
        "input_identities",
        "tables",
        "summary",
        "members",
        "complete",
        "generation_sha256",
    }
    if set(manifest) != expected_manifest_fields or manifest.get("complete") is not True:
        raise RuntimeError("HYSPLIT manifest fields or completion state changed")
    identity = {
        key: value
        for key, value in manifest.items()
        if key not in {"complete", "generation_sha256"}
    }
    generation = _hash_value(identity)
    if manifest.get("generation_sha256") != generation:
        raise RuntimeError("HYSPLIT generation identity differs")
    if require_directory_name and directory.name != generation:
        raise RuntimeError("HYSPLIT generation directory does not match its identity")

    protocol_raw = manifest.get("protocol")
    if not isinstance(protocol_raw, dict):
        raise RuntimeError("HYSPLIT manifest protocol changed")
    try:
        protocol = load_hysplit_protocol(protocol_raw)
    except ConfigError as exc:
        raise RuntimeError("HYSPLIT manifest protocol changed") from exc
    if manifest.get("ordered_receptors") != list(protocol.receptors) or manifest.get(
        "claim_boundary"
    ) != dict(protocol.claim_boundary):
        raise RuntimeError("HYSPLIT manifest claim boundary changed")

    members = manifest.get("members")
    expected_member_names = {"arrivals.parquet", "runs.parquet", "summary.json"}
    if not isinstance(members, dict) or set(members) != expected_member_names:
        raise RuntimeError("HYSPLIT member identities changed")
    for name in expected_member_names:
        if members[name] != _file_identity(directory / name):
            raise RuntimeError(f"HYSPLIT member checksum differs: {name}")

    try:
        arrivals = _normalize_plan_table(
            "arrivals", pl.read_parquet(directory / "arrivals.parquet")
        )
        runs = _normalize_plan_table("runs", pl.read_parquet(directory / "runs.parquet"))
    except pl.exceptions.PolarsError as exc:
        raise RuntimeError("HYSPLIT generation table is unreadable") from exc
    tables = manifest.get("tables")
    expected_tables = {
        "arrivals": _frame_identity(arrivals),
        "runs": _frame_identity(runs),
    }
    if tables != expected_tables:
        raise RuntimeError("HYSPLIT table identity differs")

    summary_payload = (directory / "summary.json").read_bytes()
    try:
        summary = json.loads(summary_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("HYSPLIT summary is unreadable") from exc
    if (
        not isinstance(summary, dict)
        or summary_payload != _canonical_json_bytes(summary)
        or summary != _expected_plan_summary(arrivals, runs)
        or summary != manifest.get("summary")
    ):
        raise RuntimeError("HYSPLIT summary differs from its tables")
    input_identities = manifest.get("input_identities")
    if not isinstance(input_identities, dict) or set(input_identities) != {
        "wind_frame",
        "station_geography",
    }:
        raise RuntimeError("HYSPLIT input identities changed")
    return PilotPlan(
        arrivals=arrivals,
        runs=runs,
        summary={str(key): int(value) for key, value in summary.items()},
        input_identities=input_identities,
    )


def load_hysplit_pilot_plan(directory: Path) -> PilotPlan:
    """Independently reload and verify an immutable C0 preparation generation."""
    try:
        return _load_hysplit_pilot_plan(directory.absolute(), require_directory_name=True)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("HYSPLIT generation cannot be verified") from exc


def write_hysplit_pilot_plan(
    plan: PilotPlan,
    *,
    output_root: Path | None = None,
) -> dict[str, Path]:
    """Atomically publish a content-addressed C0 preparation generation."""
    arrivals = _normalize_plan_table("arrivals", plan.arrivals)
    runs = _normalize_plan_table("runs", plan.runs)
    if plan.summary != _expected_plan_summary(arrivals, runs):
        raise RuntimeError("HYSPLIT plan summary differs from its tables")

    root = (
        output_root.absolute()
        if output_root is not None
        else outputs_dir("hysplit_pilot_plan").absolute()
    )
    generations = root / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = generations / f".hysplit-plan.staging-{token}"
    staged.mkdir()
    try:
        arrivals.write_parquet(staged / "arrivals.parquet")
        runs.write_parquet(staged / "runs.parquet")
        for name in ("arrivals.parquet", "runs.parquet"):
            with (staged / name).open("ab") as stream:
                stream.flush()
                os.fsync(stream.fileno())
        _write_bytes(staged / "summary.json", _canonical_json_bytes(plan.summary))
        members = {
            name: _file_identity(staged / name)
            for name in ("arrivals.parquet", "runs.parquet", "summary.json")
        }
        identity = _plan_identity(plan, members=members)
        generation = _hash_value(identity)
        manifest = {
            **identity,
            "complete": True,
            "generation_sha256": generation,
        }
        _write_bytes(staged / "manifest.json", _canonical_json_bytes(manifest))
        staged_plan = _load_hysplit_pilot_plan(staged, require_directory_name=False)
        if (
            not staged_plan.arrivals.equals(arrivals)
            or not staged_plan.runs.equals(runs)
            or staged_plan.summary != plan.summary
            or staged_plan.input_identities != plan.input_identities
        ):
            raise RuntimeError("HYSPLIT staged generation differs from the prepared plan")

        destination = generations / generation
        if destination.exists():
            existing = load_hysplit_pilot_plan(destination)
            if (
                not existing.arrivals.equals(arrivals)
                or not existing.runs.equals(runs)
                or existing.summary != plan.summary
                or existing.input_identities != plan.input_identities
            ):
                raise RuntimeError("existing HYSPLIT generation is not reusable")
            return _written_plan_paths(destination)
        try:
            staged.replace(destination)
        except FileExistsError as exc:
            existing = load_hysplit_pilot_plan(destination)
            if not existing.arrivals.equals(arrivals) or not existing.runs.equals(runs):
                raise RuntimeError("concurrent HYSPLIT generation differs") from exc
        load_hysplit_pilot_plan(destination)
        return _written_plan_paths(destination)
    finally:
        if (
            staged.exists()
            and staged.parent == generations
            and staged.name == f".hysplit-plan.staging-{token}"
        ):
            shutil.rmtree(staged)
