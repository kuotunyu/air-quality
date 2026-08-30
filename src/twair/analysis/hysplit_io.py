"""Strict NOAA CONTROL and trajectory-endpoint text contracts for HYSPLIT C0."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from twair.analysis.hysplit_protocol import (
    load_hysplit_protocol,
    validate_ascii_external_path,
)

_MD5 = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIAGNOSTIC_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_BASE_ENDPOINT_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "trajectory_id": pl.Int64,
    "meteorology_grid_id": pl.Int64,
    "point_utc": pl.Datetime("us", "UTC"),
    "forecast_hour": pl.Int64,
    "age_hours": pl.Float64,
    "latitude": pl.Float64,
    "longitude": pl.Float64,
    "height_m_agl": pl.Float64,
}


@dataclass(frozen=True, slots=True)
class MeteorologyFile:
    """An owner-supplied meteorology member with both upstream and local identity."""

    directory: Path
    filename: str
    md5: str
    sha256: str
    bytes: int


def _numeric(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"HYSPLIT run {label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"HYSPLIT run {label} must be finite")
    return number


def _integer(value: object, *, label: str) -> int:
    number = _numeric(value, label=label)
    if not number.is_integer():
        raise ValueError(f"HYSPLIT run {label} must be an integer")
    return int(number)


def _safe_filename(value: str, *, label: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) >= 128 or character.isspace() for character in value)
    ):
        raise ValueError(f"{label} filename must be one ASCII path component")
    return value


def _run_values(run: Mapping[str, object]) -> tuple[datetime, float, float, tuple[int, ...]]:
    protocol = load_hysplit_protocol()
    arrival = run.get("arrival_utc")
    if not isinstance(arrival, datetime) or arrival.tzinfo is None:
        raise ValueError("HYSPLIT run arrival must be timezone-aware UTC")
    arrival_utc = arrival.astimezone(UTC)
    if arrival.utcoffset() != timedelta(0):
        raise ValueError("HYSPLIT run arrival must already be UTC")
    latitude = _numeric(run.get("latitude"), label="latitude")
    longitude = _numeric(run.get("longitude"), label="longitude")
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise ValueError("HYSPLIT run coordinates are outside geographic bounds")

    raw_heights = run.get("start_heights_m_agl")
    if not isinstance(raw_heights, Sequence) or isinstance(raw_heights, (str, bytes)):
        raise ValueError("HYSPLIT run heights must be the reviewed sequence")
    heights = tuple(_integer(value, label="height") for value in raw_heights)
    if heights != protocol.start_heights_m_agl:
        raise ValueError("HYSPLIT run heights differ from the reviewed protocol")
    if _integer(run.get("duration_hours"), label="duration") != protocol.duration_hours:
        raise ValueError("HYSPLIT run duration differs from the reviewed protocol")
    if _integer(run.get("vertical_motion"), label="vertical motion") != protocol.vertical_motion:
        raise ValueError("HYSPLIT run vertical motion differs from the reviewed protocol")
    if _numeric(run.get("model_top_m_agl"), label="model top") != protocol.model_top_m_agl:
        raise ValueError("HYSPLIT run model top differs from the reviewed protocol")
    if run.get("meteorology_dataset") != protocol.meteorology_dataset:
        raise ValueError("HYSPLIT run meteorology dataset differs from the reviewed protocol")
    return arrival_utc, latitude, longitude, heights


def render_trajectory_control(
    run: Mapping[str, object],
    meteorology: Sequence[MeteorologyFile],
    *,
    output_directory: Path,
    output_filename: str,
) -> str:
    """Render the reviewed backward-trajectory CONTROL file without filesystem I/O."""
    arrival, latitude, longitude, heights = _run_values(run)
    if not meteorology:
        raise ValueError("HYSPLIT CONTROL requires at least one meteorology file")
    output = validate_ascii_external_path(
        output_directory,
        label="HYSPLIT output directory",
    )
    endpoint_name = _safe_filename(output_filename, label="HYSPLIT output")

    names: set[str] = set()
    validated: list[tuple[Path, str]] = []
    for member in meteorology:
        directory = validate_ascii_external_path(
            member.directory,
            label="HYSPLIT meteorology directory",
        )
        filename = _safe_filename(member.filename, label="HYSPLIT meteorology")
        if filename in names:
            raise ValueError(f"HYSPLIT meteorology filename is duplicate: {filename}")
        names.add(filename)
        if _MD5.fullmatch(member.md5) is None or _SHA256.fullmatch(member.sha256) is None:
            raise ValueError(f"HYSPLIT meteorology identity is invalid: {filename}")
        if isinstance(member.bytes, bool) or member.bytes <= 0:
            raise ValueError(f"HYSPLIT meteorology byte size is invalid: {filename}")
        validated.append((directory, filename))

    protocol = load_hysplit_protocol()
    lines = [
        arrival.strftime("%y %m %d %H %M"),
        str(len(heights)),
        *(f"{latitude:.6f} {longitude:.6f} {height}" for height in heights),
        str(protocol.duration_hours),
        str(protocol.vertical_motion),
        f"{protocol.model_top_m_agl:.1f}",
        f"1 {len(validated)}",
    ]
    for directory, filename in validated:
        lines.extend((f"{directory.as_posix().rstrip('/')}/", filename))
    lines.extend((f"{output.as_posix().rstrip('/')}/", endpoint_name))
    return "\n".join([*lines, ""])


def _parse_int(token: str, *, label: str) -> int:
    try:
        return int(token)
    except ValueError as exc:
        raise RuntimeError(f"HYSPLIT {label} must be an integer") from exc


def _parse_float(token: str, *, label: str) -> float:
    try:
        return float(token)
    except ValueError as exc:
        raise RuntimeError(f"HYSPLIT {label} must be numeric") from exc


def _year(value: int) -> int:
    if 0 <= value <= 49:
        return 2000 + value
    if 50 <= value <= 99:
        return 1900 + value
    return value


def _utc(parts: Sequence[int], *, label: str) -> datetime:
    try:
        if len(parts) == 4:
            return datetime(_year(parts[0]), parts[1], parts[2], parts[3], tzinfo=UTC)
        if len(parts) == 5:
            return datetime(_year(parts[0]), parts[1], parts[2], parts[3], parts[4], tzinfo=UTC)
        raise ValueError("unexpected timestamp width")
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"HYSPLIT {label} timestamp is invalid") from exc


def _finite_coordinates(latitude: float, longitude: float, height: float, *, label: str) -> None:
    if not all(math.isfinite(value) for value in (latitude, longitude, height)):
        raise RuntimeError(f"HYSPLIT {label} requires finite coordinates")
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise RuntimeError(f"HYSPLIT {label} coordinates are outside geographic bounds")


def parse_trajectory_endpoints(text: str) -> pl.DataFrame:
    """Parse and structurally validate supported NOAA S263 endpoint formats."""
    lines = text.strip().splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise RuntimeError("HYSPLIT endpoint file is empty or contains blank records")
    cursor = 0

    record_one = lines[cursor].split()
    cursor += 1
    if len(record_one) != 2:
        raise RuntimeError("HYSPLIT endpoint record 1 must contain grid count and format version")
    grid_count = _parse_int(record_one[0], label="meteorology grid count")
    format_version = _parse_int(record_one[1], label="endpoint format version")
    if grid_count < 1 or format_version not in {1, 2}:
        raise RuntimeError("HYSPLIT endpoint requires meteorology and format version 1 or 2")

    for _ in range(grid_count):
        if cursor >= len(lines):
            raise RuntimeError("HYSPLIT endpoint meteorology record is missing")
        record = lines[cursor].split()
        cursor += 1
        if len(record) != 6 or not record[0]:
            raise RuntimeError("HYSPLIT endpoint meteorology record is malformed")
        values = [_parse_int(token, label="meteorology start") for token in record[1:]]
        _utc(values[:4], label="meteorology start")

    if cursor >= len(lines):
        raise RuntimeError("HYSPLIT endpoint trajectory header is missing")
    trajectory_header = lines[cursor].split()
    cursor += 1
    if len(trajectory_header) != 3:
        raise RuntimeError("HYSPLIT endpoint trajectory header is malformed")
    trajectory_count = _parse_int(trajectory_header[0], label="trajectory count")
    if trajectory_count < 1 or trajectory_header[1].upper() != "BACKWARD":
        raise RuntimeError("HYSPLIT endpoint must contain backward trajectories")

    for _ in range(trajectory_count):
        if cursor >= len(lines):
            raise RuntimeError("HYSPLIT endpoint starting-location record is missing")
        record = lines[cursor].split()
        cursor += 1
        if len(record) != 7:
            raise RuntimeError("HYSPLIT endpoint starting-location record is malformed")
        start = [_parse_int(token, label="trajectory start") for token in record[:4]]
        _utc(start, label="trajectory start")
        latitude, longitude, height = (
            _parse_float(record[4], label="starting latitude"),
            _parse_float(record[5], label="starting longitude"),
            _parse_float(record[6], label="starting height"),
        )
        _finite_coordinates(latitude, longitude, height, label="starting location")

    if cursor >= len(lines):
        raise RuntimeError("HYSPLIT endpoint diagnostic record is missing")
    diagnostic_record = lines[cursor].split()
    cursor += 1
    if not diagnostic_record:
        raise RuntimeError("HYSPLIT endpoint diagnostic record is missing")
    diagnostic_count = _parse_int(diagnostic_record[0], label="diagnostic count")
    labels = diagnostic_record[1:]
    if diagnostic_count < 1 or len(labels) != diagnostic_count or labels[0].upper() != "PRESSURE":
        raise RuntimeError("HYSPLIT endpoint first diagnostic must be PRESSURE")
    if len({label.lower() for label in labels}) != len(labels) or any(
        _DIAGNOSTIC_LABEL.fullmatch(label) is None for label in labels
    ):
        raise RuntimeError("HYSPLIT endpoint diagnostic labels are invalid or duplicate")
    columns = [label.lower() for label in labels]

    rows: list[dict[str, object]] = []
    expected_fields = 12 + diagnostic_count
    for line in lines[cursor:]:
        record = line.split()
        if len(record) != expected_fields:
            raise RuntimeError("HYSPLIT endpoint record is malformed")
        trajectory_id = _parse_int(record[0], label="trajectory ID")
        grid_id = _parse_int(record[1], label="meteorology grid ID")
        point_parts = [_parse_int(token, label="endpoint time") for token in record[2:7]]
        point_utc = _utc(point_parts, label="endpoint")
        forecast_hour = _parse_int(record[7], label="forecast hour")
        age = _parse_float(record[8], label="trajectory age")
        latitude = _parse_float(record[9], label="endpoint latitude")
        longitude = _parse_float(record[10], label="endpoint longitude")
        height = _parse_float(record[11], label="endpoint height")
        _finite_coordinates(latitude, longitude, height, label="endpoint")
        if trajectory_id < 1 or trajectory_id > trajectory_count:
            raise RuntimeError("HYSPLIT endpoint has an unexpected trajectory ID")
        if grid_id < 1 or grid_id > grid_count:
            raise RuntimeError("HYSPLIT endpoint refers to missing meteorology")
        diagnostics = [
            _parse_float(token, label=f"diagnostic {label}")
            for token, label in zip(record[12:], columns, strict=True)
        ]
        if not all(math.isfinite(value) for value in diagnostics):
            raise RuntimeError("HYSPLIT endpoint diagnostic value must be finite")
        row: dict[str, object] = {
            "trajectory_id": trajectory_id,
            "meteorology_grid_id": grid_id,
            "point_utc": point_utc,
            "forecast_hour": forecast_hour,
            "age_hours": age,
            "latitude": latitude,
            "longitude": longitude,
            "height_m_agl": height,
        }
        row.update(dict(zip(columns, diagnostics, strict=True)))
        rows.append(row)
    if not rows:
        raise RuntimeError("HYSPLIT endpoint contains no endpoint records")

    schema = {
        **_BASE_ENDPOINT_SCHEMA,
        **dict.fromkeys(columns, pl.Float64),
    }
    frame = pl.DataFrame(rows, schema=schema)
    present_ids = set(frame["trajectory_id"].unique().to_list())
    if present_ids != set(range(1, trajectory_count + 1)):
        raise RuntimeError("HYSPLIT endpoint has missing or unexpected trajectory IDs")
    duplicate_age = frame.group_by("trajectory_id", "age_hours").len().filter(pl.col("len") > 1)
    if not duplicate_age.is_empty():
        raise RuntimeError("HYSPLIT endpoint has a duplicate trajectory age")
    return frame


def validate_complete_trajectory(frame: pl.DataFrame, *, duration_hours: int) -> None:
    """Require complete hourly backward trajectories from age zero to duration."""
    if isinstance(duration_hours, bool) or duration_hours >= 0:
        raise RuntimeError("HYSPLIT completion duration must be negative")
    required = {*_BASE_ENDPOINT_SCHEMA, "pressure"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"HYSPLIT endpoint is missing required columns: {sorted(missing)}")
    if frame.is_empty():
        raise RuntimeError("HYSPLIT endpoint is empty")
    if frame.filter(
        ~pl.col("latitude").is_finite()
        | ~pl.col("longitude").is_finite()
        | ~pl.col("height_m_agl").is_finite()
    ).height:
        raise RuntimeError("HYSPLIT endpoint requires finite coordinates")
    ids = sorted(frame["trajectory_id"].unique().to_list())
    if ids != list(range(1, len(ids) + 1)):
        raise RuntimeError("HYSPLIT endpoint has unexpected trajectory IDs")

    expected_ages = [float(value) for value in range(0, duration_hours - 1, -1)]
    for trajectory_id in ids:
        trajectory = frame.filter(pl.col("trajectory_id") == trajectory_id)
        ages = trajectory["age_hours"].to_list()
        if any(not math.isfinite(age) for age in ages):
            raise RuntimeError("HYSPLIT trajectory age must be finite")
        if any(age > 0 for age in ages):
            raise RuntimeError("HYSPLIT backward trajectory contains a positive age")
        if any(not float(age).is_integer() for age in ages):
            raise RuntimeError("HYSPLIT trajectory age must be an integer hour")
        if ages != expected_ages:
            raise RuntimeError("HYSPLIT trajectory is not complete and monotonically decreasing")
        points = trajectory["point_utc"].to_list()
        start = points[0]
        if any(
            point != start + timedelta(hours=age)
            for point, age in zip(points, expected_ages, strict=True)
        ):
            raise RuntimeError("HYSPLIT trajectory timestamps disagree with their ages")
