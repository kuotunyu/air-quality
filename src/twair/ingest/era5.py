"""Acquire bounded ERA5 fields without deriving or repairing measurements."""

from __future__ import annotations

import calendar
import json
import math
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import netCDF4
import numpy as np
import polars as pl

from twair.config import ConfigError, load_conf
from twair.geometry import haversine_km
from twair.ingest.station_inventory import (
    station_inventory_generation,
    validate_generation_sha256,
)
from twair.paths import interim_dir
from twair.provenance import git_state

__all__ = [
    "CdsEra5Backend",
    "Era5Grid",
    "Era5Result",
    "Era5Source",
    "Era5Variable",
    "acquire_era5",
    "build_era5_request",
    "load_era5_source",
    "read_era5_grid",
    "read_era5_result",
    "sample_era5_stations",
]


@dataclass(frozen=True, slots=True)
class Era5Variable:
    output_name: str
    request_name: str
    netcdf_name: str
    unit: str


@dataclass(frozen=True, slots=True)
class Era5Source:
    dataset: str
    product_type: str
    data_format: str
    download_format: str
    area: tuple[float, float, float, float]
    variables: tuple[Era5Variable, ...]


@dataclass(frozen=True, slots=True)
class Era5Grid:
    times: tuple[datetime, ...]
    latitudes: np.ndarray
    longitudes: np.ndarray
    values: dict[str, np.ma.MaskedArray]


@dataclass(frozen=True, slots=True)
class Era5Result:
    values: pl.DataFrame
    coverage: pl.DataFrame
    manifest: dict[str, Any]


class Era5Backend(Protocol):
    def retrieve(
        self,
        dataset: str,
        request: dict[str, object],
        target: Path,
    ) -> None: ...


class CdsEra5Backend:
    def __init__(self, url: str, key: str) -> None:
        import cdsapi

        self._client = cdsapi.Client(url=url, key=key, quiet=True, progress=False)

    def retrieve(
        self,
        dataset: str,
        request: dict[str, object],
        target: Path,
    ) -> None:
        self._client.retrieve(dataset, request, str(target))


def _nonempty_text(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} must be a non-empty string")
    return value.strip()


def load_era5_source(config: dict[str, Any] | None = None) -> Era5Source:
    raw = config if config is not None else load_conf("era5")
    group = raw.get("single_levels")
    if not isinstance(group, dict):
        raise ConfigError("conf/era5.yaml must define a `single_levels` mapping")

    text_fields = {
        name: _nonempty_text(group.get(name), path=f"era5.single_levels.{name}")
        for name in ("dataset", "product_type", "data_format", "download_format")
    }
    raw_area = group.get("area")
    if (
        not isinstance(raw_area, list)
        or len(raw_area) != 4
        or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_area)
    ):
        raise ConfigError("era5.single_levels.area must be [north, west, south, east]")
    area = (
        float(raw_area[0]),
        float(raw_area[1]),
        float(raw_area[2]),
        float(raw_area[3]),
    )
    north, west, south, east = area
    if (
        not all(math.isfinite(value) for value in area)
        or not -90 <= south < north <= 90
        or not -180 <= west < east <= 180
    ):
        raise ConfigError("era5.single_levels.area has invalid or inverted bounds")

    raw_variables = group.get("variables")
    if not isinstance(raw_variables, dict) or not raw_variables:
        raise ConfigError("era5.single_levels.variables must be a non-empty mapping")
    variables: list[Era5Variable] = []
    for output_name, payload in raw_variables.items():
        if (
            not isinstance(output_name, str)
            or not output_name.strip()
            or output_name != output_name.strip()
        ):
            raise ConfigError("every ERA5 output name must be an unpadded non-empty string")
        if not isinstance(payload, dict):
            raise ConfigError(f"era5.single_levels.variables.{output_name} must be a mapping")
        variables.append(
            Era5Variable(
                output_name=output_name,
                request_name=_nonempty_text(
                    payload.get("request_name"),
                    path=f"era5.single_levels.variables.{output_name}.request_name",
                ),
                netcdf_name=_nonempty_text(
                    payload.get("netcdf_name"),
                    path=f"era5.single_levels.variables.{output_name}.netcdf_name",
                ),
                unit=_nonempty_text(
                    payload.get("unit"),
                    path=f"era5.single_levels.variables.{output_name}.unit",
                ),
            )
        )
    for field_name, values in {
        "output_name": [item.output_name for item in variables],
        "request_name": [item.request_name for item in variables],
        "netcdf_name": [item.netcdf_name for item in variables],
    }.items():
        if len(values) != len(set(values)):
            raise ConfigError(f"ERA5 variable {field_name} values must be unique")

    return Era5Source(
        dataset=text_fields["dataset"],
        product_type=text_fields["product_type"],
        data_format=text_fields["data_format"],
        download_format=text_fields["download_format"],
        area=area,
        variables=tuple(variables),
    )


def build_era5_request(source: Era5Source, *, year: int, month: int) -> dict[str, object]:
    if isinstance(year, bool) or not isinstance(year, int) or year < 1940:
        raise ValueError("ERA5 year must be an integer from 1940 onward")
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise ValueError("ERA5 month must be an integer from 1 through 12")
    days = calendar.monthrange(year, month)[1]
    return {
        "product_type": [source.product_type],
        "variable": [item.request_name for item in source.variables],
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": [f"{day:02d}" for day in range(1, days + 1)],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "data_format": source.data_format,
        "download_format": source.download_format,
        "area": list(source.area),
    }


def _strict_coordinate(values: object, *, name: str, descending: bool) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1 or array.size == 0 or not np.isfinite(array).all():
        raise RuntimeError(f"ERA5 {name} coordinate must be a finite one-dimensional array")
    changes = np.diff(array)
    if changes.size and (np.any(changes >= 0) if descending else np.any(changes <= 0)):
        direction = "descending" if descending else "ascending"
        raise RuntimeError(f"ERA5 {name} coordinate must be strictly {direction}")
    if changes.size > 1 and not np.allclose(changes, changes[0], rtol=0, atol=1e-10):
        raise RuntimeError(f"ERA5 {name} coordinate is not regular")
    return array


def _python_utc(value: Any) -> datetime:
    if not all(
        hasattr(value, name) for name in ("year", "month", "day", "hour", "minute", "second")
    ):
        raise RuntimeError("ERA5 valid_time could not be decoded as a calendar timestamp")
    return datetime(
        int(value.year),
        int(value.month),
        int(value.day),
        int(value.hour),
        int(value.minute),
        int(value.second),
        tzinfo=UTC,
    )


def read_era5_grid(path: Path, source: Era5Source, *, year: int, month: int) -> Era5Grid:
    try:
        dataset = netCDF4.Dataset("inmemory.nc", memory=path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"ERA5 file is not a readable NetCDF dataset: {path}") from exc
    try:
        for coordinate in ("valid_time", "latitude", "longitude"):
            if coordinate not in dataset.variables:
                raise RuntimeError(f"ERA5 file is missing {coordinate}")
        time_variable = dataset.variables["valid_time"]
        units = getattr(time_variable, "units", None)
        calendar_name = getattr(time_variable, "calendar", "standard")
        if not isinstance(units, str) or not units:
            raise RuntimeError("ERA5 valid_time has no units")
        decoded = netCDF4.num2date(
            time_variable[:],
            units=units,
            calendar=calendar_name,
            only_use_cftime_datetimes=False,
            only_use_python_datetimes=True,
        )
        decoded_values = np.asarray(decoded, dtype=object).reshape(-1)
        times = tuple(_python_utc(value) for value in decoded_values)
        if not times or len(set(times)) != len(times):
            raise RuntimeError("ERA5 hour keys are empty or duplicated")
        if any(
            value.minute or value.second or value.year != year or value.month != month
            for value in times
        ):
            raise RuntimeError("ERA5 hour keys fall outside the requested month or hourly boundary")
        if any(next_value <= value for value, next_value in pairwise(times)):
            raise RuntimeError("ERA5 hour keys are not strictly increasing")
        if any(
            (next_value - value).total_seconds() != 3600 for value, next_value in pairwise(times)
        ):
            raise RuntimeError("ERA5 hour keys are not continuous hourly observations")

        latitudes = _strict_coordinate(
            dataset.variables["latitude"][:], name="latitude", descending=True
        )
        longitudes = _strict_coordinate(
            dataset.variables["longitude"][:], name="longitude", descending=False
        )
        expected_shape = (len(times), latitudes.size, longitudes.size)
        values: dict[str, np.ma.MaskedArray] = {}
        for variable in source.variables:
            if variable.netcdf_name not in dataset.variables:
                raise RuntimeError(f"ERA5 file is missing {variable.netcdf_name}")
            raw_variable = dataset.variables[variable.netcdf_name]
            if tuple(raw_variable.dimensions) != ("valid_time", "latitude", "longitude"):
                raise RuntimeError(f"ERA5 {variable.netcdf_name} dimensions changed")
            if getattr(raw_variable, "units", None) != variable.unit:
                raise RuntimeError(f"ERA5 {variable.netcdf_name} unit changed")
            array = np.ma.asarray(raw_variable[:], dtype=np.float64).copy()
            if array.shape != expected_shape:
                raise RuntimeError(f"ERA5 {variable.netcdf_name} shape changed")
            unmasked = np.asarray(array.data)[~np.ma.getmaskarray(array)]
            if not np.isfinite(unmasked).all():
                raise RuntimeError(f"ERA5 {variable.netcdf_name} has an unmasked non-finite value")
            values[variable.output_name] = array
    finally:
        dataset.close()
    return Era5Grid(times=times, latitudes=latitudes, longitudes=longitudes, values=values)


def sample_era5_stations(grid: Era5Grid, stations: pl.DataFrame) -> pl.DataFrame:
    required = {"station_name", "lon", "lat"}
    missing = required - set(stations.columns)
    if missing:
        raise RuntimeError(f"station table is missing {sorted(missing)}")
    if stations.is_empty():
        raise RuntimeError("station table is empty")
    duplicates = stations.group_by("station_name").len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise RuntimeError(f"station names are not unique: {duplicates['station_name'].to_list()}")
    invalid = stations.filter(
        pl.col("station_name").is_null()
        | pl.col("lon").is_null()
        | pl.col("lat").is_null()
        | ~pl.col("lon").cast(pl.Float64).is_finite()
        | ~pl.col("lat").cast(pl.Float64).is_finite()
    )
    if not invalid.is_empty():
        raise RuntimeError("every ERA5 pilot station must have a name and finite coordinates")

    records: list[dict[str, object]] = []
    for station in stations.select("station_name", "lon", "lat").iter_rows(named=True):
        station_name = str(station["station_name"])
        station_lon = float(station["lon"])
        station_lat = float(station["lat"])
        latitude_index = int(np.argmin(np.abs(grid.latitudes - station_lat)))
        longitude_index = int(np.argmin(np.abs(grid.longitudes - station_lon)))
        grid_lat = float(grid.latitudes[latitude_index])
        grid_lon = float(grid.longitudes[longitude_index])
        distance = haversine_km(station_lat, station_lon, grid_lat, grid_lon)
        for time_index, timestamp in enumerate(grid.times):
            record: dict[str, object] = {
                "station_name": station_name,
                "ts_utc": timestamp,
                "grid_lat": grid_lat,
                "grid_lon": grid_lon,
                "grid_distance_km": distance,
            }
            for output_name, array in grid.values.items():
                value = array[time_index, latitude_index, longitude_index]
                record[output_name] = None if np.ma.is_masked(value) else float(value)
            records.append(record)

    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        "station_name": pl.String,
        "ts_utc": pl.Datetime("us", "UTC"),
        "grid_lat": pl.Float64,
        "grid_lon": pl.Float64,
        "grid_distance_km": pl.Float64,
    }
    schema.update(dict.fromkeys(grid.values, pl.Float64))
    return pl.from_dicts(records, schema=schema).sort("station_name", "ts_utc")


_VALUE_FILENAME = "era5_station_hour.parquet"
_COVERAGE_FILENAME = "era5_coverage.parquet"
_MANIFEST_FILENAME = "manifest.json"

_COVERAGE_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "month": pl.Date,
    "variable": pl.String,
    "unit": pl.String,
    "n_hours": pl.Int64,
    "n_stations": pl.Int64,
    "n_valid": pl.Int64,
    "n_null": pl.Int64,
}


def _value_schema(source: Era5Source) -> dict[str, pl.DataType | type[pl.DataType]]:
    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        "station_name": pl.String,
        "ts_utc": pl.Datetime("us", "UTC"),
        "grid_lat": pl.Float64,
        "grid_lon": pl.Float64,
        "grid_distance_km": pl.Float64,
    }
    schema.update(dict.fromkeys((item.output_name for item in source.variables), pl.Float64))
    return schema


def _source_contract(source: Era5Source) -> dict[str, object]:
    return {
        "dataset": source.dataset,
        "product_type": source.product_type,
        "data_format": source.data_format,
        "download_format": source.download_format,
        "area": list(source.area),
        "variables": [
            {
                "output_name": item.output_name,
                "request_name": item.request_name,
                "netcdf_name": item.netcdf_name,
                "unit": item.unit,
            }
            for item in source.variables
        ],
    }


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _validated_months(months: tuple[int, ...]) -> tuple[int, ...]:
    if not months or any(
        isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12
        for month in months
    ):
        raise ValueError("months must be integers from 1 through 12")
    if len(months) != len(set(months)):
        raise ValueError("months must not contain duplicates")
    return tuple(sorted(months))


def _expected_month_times(year: int, month: int) -> tuple[datetime, ...]:
    first = datetime(year, month, 1, tzinfo=UTC)
    n_hours = calendar.monthrange(year, month)[1] * 24
    return tuple(first + timedelta(hours=offset) for offset in range(n_hours))


def _require_complete_month(grid: Era5Grid, *, year: int, month: int) -> None:
    if grid.times != _expected_month_times(year, month):
        raise RuntimeError(f"ERA5 {year}-{month:02d} does not contain every requested hour")


def _coverage(values: pl.DataFrame, source: Era5Source, *, year: int) -> pl.DataFrame:
    records: list[dict[str, object]] = []
    months = sorted(values["ts_utc"].dt.month().unique().to_list())
    for month in months:
        monthly = values.filter(pl.col("ts_utc").dt.month() == month)
        n_stations = monthly["station_name"].n_unique()
        n_hours = monthly["ts_utc"].n_unique()
        for variable in source.variables:
            n_null = monthly[variable.output_name].null_count()
            records.append(
                {
                    "month": date(year, month, 1),
                    "variable": variable.output_name,
                    "unit": variable.unit,
                    "n_hours": n_hours,
                    "n_stations": n_stations,
                    "n_valid": monthly.height - n_null,
                    "n_null": n_null,
                }
            )
    return pl.from_dicts(records, schema=_COVERAGE_SCHEMA).sort("month", "variable")


def _era5_generation_dir(year: int, generation_sha256: str) -> Path:
    generation = validate_generation_sha256(generation_sha256)
    if isinstance(year, bool) or not isinstance(year, int) or year <= 0:
        raise ValueError("ERA5 generation year must be a positive integer")
    return interim_dir("era5") / "generations" / generation / f"year={year}"


def _validate_generation_destination(
    destination: Path,
    *,
    year: int,
    generation_sha256: str,
) -> None:
    generation = validate_generation_sha256(generation_sha256)
    if (
        destination.name != f"year={year}"
        or destination.parent.name != generation
        or destination.parent.parent.name != "generations"
    ):
        raise RuntimeError("ERA5 destination generation does not match the station inventory")


def _recover_interrupted_swap(destination: Path) -> Path | None:
    parent = destination.parent
    if not parent.exists():
        return None
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1:
        raise RuntimeError(f"multiple interrupted ERA5 backups found beside {destination}")
    if destination.exists() and backups and stages:
        raise RuntimeError(f"ambiguous interrupted ERA5 swap found beside {destination}")
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    return backups[0] if backups else None


def _manifest_mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"ERA5 {label} must be a mapping")
    return value


def _manifest_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"ERA5 {label} must be an exact integer")
    return value


def _validate_result(
    result: Era5Result,
    source: Era5Source,
    *,
    destination: Path | None = None,
) -> None:
    if result.values.schema != pl.Schema(_value_schema(source)):
        raise RuntimeError("ERA5 station-hour schema does not match the source contract")
    if result.coverage.schema != pl.Schema(_COVERAGE_SCHEMA):
        raise RuntimeError("ERA5 coverage schema does not match the source contract")
    if result.values.is_empty():
        raise RuntimeError("ERA5 station-hour result is empty")
    if result.values.unique(["station_name", "ts_utc"]).height != result.values.height:
        raise RuntimeError("ERA5 station-hour keys are duplicated")

    manifest = result.manifest
    if manifest.get("schema_version") != 1:
        raise RuntimeError("ERA5 manifest schema version is unsupported")
    year = _manifest_int(manifest.get("year"), label="manifest year")
    months_value = manifest.get("months")
    if not isinstance(months_value, list) or any(
        isinstance(month, bool) or not isinstance(month, int) for month in months_value
    ):
        raise RuntimeError("ERA5 manifest months must be exact integers")
    months = _validated_months(tuple(months_value))
    if months_value != list(months):
        raise RuntimeError("ERA5 manifest months must be sorted and unique")
    observed_months = tuple(sorted(result.values["ts_utc"].dt.month().unique().to_list()))
    if observed_months != months or result.values["ts_utc"].dt.year().unique().to_list() != [year]:
        raise RuntimeError("ERA5 station-hour keys do not match the manifest year/months")
    station_count = _manifest_int(
        manifest.get("stations_with_coordinates"),
        label="stations_with_coordinates",
    )
    stations_total = _manifest_int(manifest.get("stations_total"), label="stations_total")
    stations_without_coordinates = _manifest_int(
        manifest.get("stations_without_coordinates"),
        label="stations_without_coordinates",
    )
    if station_count + stations_without_coordinates != stations_total:
        raise RuntimeError("ERA5 manifest station counts are inconsistent")
    if result.values["station_name"].n_unique() != station_count:
        raise RuntimeError("ERA5 station count is inconsistent")
    invalid_grid = result.values.filter(
        pl.any_horizontal(
            pl.col("grid_lat").is_null(),
            pl.col("grid_lon").is_null(),
            pl.col("grid_distance_km").is_null(),
            ~pl.col("grid_lat").is_finite(),
            ~pl.col("grid_lon").is_finite(),
            ~pl.col("grid_distance_km").is_finite(),
        )
    )
    if not invalid_grid.is_empty():
        raise RuntimeError("ERA5 selected grid coordinates or distances are not finite")
    for variable in source.variables:
        invalid_values = result.values.filter(
            pl.col(variable.output_name).is_not_null() & ~pl.col(variable.output_name).is_finite()
        )
        if not invalid_values.is_empty():
            raise RuntimeError(f"ERA5 {variable.output_name} has an unmasked non-finite value")
    for month in months:
        monthly = result.values.filter(pl.col("ts_utc").dt.month() == month)
        expected_rows = station_count * len(_expected_month_times(year, month))
        if monthly.height != expected_rows:
            raise RuntimeError(f"ERA5 month {month} has incomplete station-hour keys")

    expected_coverage = _coverage(result.values, source, year=year)
    if not result.coverage.equals(expected_coverage):
        raise RuntimeError("ERA5 coverage does not match the station-hour values")
    if manifest.get("rows") != result.values.height:
        raise RuntimeError("ERA5 manifest row count is inconsistent")
    expected_nulls = {
        item.output_name: result.values[item.output_name].null_count() for item in source.variables
    }
    if manifest.get("null_values") != expected_nulls:
        raise RuntimeError("ERA5 manifest null counts are inconsistent")
    contract = _source_contract(source)
    if manifest.get("source_contract") != contract:
        raise RuntimeError("ERA5 source contract changed")
    if manifest.get("source_contract_sha256") != _canonical_sha256(contract):
        raise RuntimeError("ERA5 source contract checksum is inconsistent")
    generation = manifest.get("inventory_generation_sha256")
    if not isinstance(generation, str):
        raise RuntimeError("ERA5 manifest has no station inventory generation")
    validate_generation_sha256(generation)
    if destination is not None:
        _validate_generation_destination(
            destination,
            year=year,
            generation_sha256=generation,
        )


def _read_result_dir(
    destination: Path,
    source: Era5Source,
    *,
    validate_destination: bool,
) -> Era5Result:
    if not destination.exists():
        raise FileNotFoundError(f"ERA5 result not found: {destination}")
    if not destination.is_dir():
        raise RuntimeError(f"ERA5 destination is not a directory: {destination}")
    expected_top = {"raw", _VALUE_FILENAME, _COVERAGE_FILENAME, _MANIFEST_FILENAME}
    present_top = {path.name for path in destination.iterdir()}
    if present_top != expected_top:
        raise RuntimeError(
            f"ERA5 destination must contain exactly {sorted(expected_top)}, "
            f"found {sorted(present_top)}"
        )
    try:
        manifest = json.loads((destination / _MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("ERA5 manifest is not readable JSON") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("ERA5 manifest must be a JSON object")
    raw_files = _manifest_mapping(manifest.get("raw_files"), label="raw_files")
    months_value = manifest.get("months")
    if not isinstance(months_value, list) or any(
        isinstance(month, bool) or not isinstance(month, int) for month in months_value
    ):
        raise RuntimeError("ERA5 manifest months must be exact integers")
    months = _validated_months(tuple(months_value))
    if months_value != list(months):
        raise RuntimeError("ERA5 manifest months must be sorted and unique")
    expected_raw_keys = {f"{month:02d}" for month in months}
    if set(raw_files) != expected_raw_keys:
        raise RuntimeError("ERA5 raw file inventory does not match the manifest months")
    expected_raw_names = {f"month={month:02d}.nc" for month in months}
    raw_dir = destination / "raw"
    if not raw_dir.is_dir() or {path.name for path in raw_dir.iterdir()} != expected_raw_names:
        raise RuntimeError("ERA5 raw directory does not match the manifest months")
    for month in months:
        key = f"{month:02d}"
        entry = _manifest_mapping(raw_files.get(key), label=f"raw_files.{key}")
        relative_path = f"raw/month={key}.nc"
        if entry.get("path") != relative_path:
            raise RuntimeError(f"ERA5 raw month {key} path is inconsistent")
        raw_path = destination / relative_path
        payload = raw_path.read_bytes()
        if entry.get("sha256") != sha256(payload).hexdigest():
            raise RuntimeError(f"ERA5 raw month {key} checksum is inconsistent")
        if entry.get("bytes") != len(payload):
            raise RuntimeError(f"ERA5 raw month {key} byte count is inconsistent")
        year = _manifest_int(manifest.get("year"), label="manifest year")
        if entry.get("request") != build_era5_request(source, year=year, month=month):
            raise RuntimeError(f"ERA5 raw month {key} request contract changed")

    result = Era5Result(
        values=pl.read_parquet(destination / _VALUE_FILENAME),
        coverage=pl.read_parquet(destination / _COVERAGE_FILENAME),
        manifest=manifest,
    )
    _validate_result(
        result,
        source,
        destination=destination if validate_destination else None,
    )
    return result


def read_era5_result(destination: Path, source: Era5Source | None = None) -> Era5Result:
    return _read_result_dir(
        destination,
        source or load_era5_source(),
        validate_destination=True,
    )


def acquire_era5(
    stations: pl.DataFrame,
    *,
    backend: Era5Backend,
    year: int,
    months: tuple[int, ...],
    inventory_generation: bool,
    confirm_download: bool,
    destination: Path | None = None,
    generated_at: str | None = None,
    source: Era5Source | None = None,
) -> Era5Result:
    if not confirm_download:
        raise RuntimeError("ERA5 acquisition requires --confirm-download")
    if not inventory_generation:
        raise RuntimeError("ERA5 acquisition requires --inventory-generation")
    selected_months = _validated_months(months)
    selected_source = source or load_era5_source()
    generation = station_inventory_generation(stations)
    north, west, south, east = selected_source.area
    outside = generation.stations.filter(
        ~pl.col("lon").is_between(west, east) | ~pl.col("lat").is_between(south, north)
    )
    if not outside.is_empty():
        raise RuntimeError(
            "reviewed station coordinates are outside the ERA5 request area: "
            f"{outside['station_name'].to_list()}"
        )
    out = destination or _era5_generation_dir(year, generation.sha256)
    _validate_generation_destination(
        out,
        year=year,
        generation_sha256=generation.sha256,
    )

    stale_backup = _recover_interrupted_swap(out)
    existing = (
        _read_result_dir(out, selected_source, validate_destination=True) if out.exists() else None
    )
    if stale_backup is not None:
        shutil.rmtree(stale_backup)
    existing_months = set(existing.manifest["months"]) if existing is not None else set()
    all_months = tuple(sorted(existing_months | set(selected_months)))

    out.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = out.with_name(f".{out.name}.staging-{token}")
    backup = out.with_name(f".{out.name}.backup-{token}")
    raw_dir = staged / "raw"
    raw_dir.mkdir(parents=True)
    downloaded: list[int] = []
    reused: list[int] = []
    try:
        if existing is not None:
            for month in existing_months:
                shutil.copyfile(
                    out / "raw" / f"month={month:02d}.nc",
                    raw_dir / f"month={month:02d}.nc",
                )
        for month in selected_months:
            raw_path = raw_dir / f"month={month:02d}.nc"
            if month in existing_months:
                reused.append(month)
                continue
            backend.retrieve(
                selected_source.dataset,
                build_era5_request(selected_source, year=year, month=month),
                raw_path,
            )
            if not raw_path.is_file():
                raise RuntimeError(f"CDS did not write ERA5 month {month:02d}")
            downloaded.append(month)

        frames: list[pl.DataFrame] = []
        raw_manifest: dict[str, object] = {}
        for month in all_months:
            raw_path = raw_dir / f"month={month:02d}.nc"
            grid = read_era5_grid(raw_path, selected_source, year=year, month=month)
            _require_complete_month(grid, year=year, month=month)
            frames.append(sample_era5_stations(grid, generation.stations))
            payload = raw_path.read_bytes()
            raw_manifest[f"{month:02d}"] = {
                "path": f"raw/month={month:02d}.nc",
                "bytes": len(payload),
                "sha256": sha256(payload).hexdigest(),
                "request": build_era5_request(selected_source, year=year, month=month),
            }
        values = pl.concat(frames).sort("station_name", "ts_utc")
        coverage = _coverage(values, selected_source, year=year)
        contract = _source_contract(selected_source)
        run_generated_at = generated_at or datetime.now(UTC).isoformat(timespec="seconds")
        git_sha, git_dirty = git_state()
        prior_runs = list(existing.manifest.get("acquisition_runs", [])) if existing else []
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "generated_at": run_generated_at,
            "year": year,
            "months": list(all_months),
            "stations_total": generation.stations_total,
            "stations_with_coordinates": generation.stations_with_coordinates,
            "stations_without_coordinates": generation.stations_without_coordinates,
            "inventory_generation_sha256": generation.sha256,
            "source_contract": contract,
            "source_contract_sha256": _canonical_sha256(contract),
            "raw_files": raw_manifest,
            "rows": values.height,
            "null_values": {
                item.output_name: values[item.output_name].null_count()
                for item in selected_source.variables
            },
            "git_sha": git_sha,
            "git_dirty": git_dirty,
            "acquisition_runs": [
                *prior_runs,
                {
                    "generated_at": run_generated_at,
                    "requested_months": list(selected_months),
                    "downloaded_months": downloaded,
                    "reused_months": reused,
                    "git_sha": git_sha,
                    "git_dirty": git_dirty,
                },
            ],
        }
        result = Era5Result(values=values, coverage=coverage, manifest=manifest)
        _validate_result(result, selected_source)
        values.write_parquet(staged / _VALUE_FILENAME)
        coverage.write_parquet(staged / _COVERAGE_FILENAME)
        (staged / _MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _read_result_dir(staged, selected_source, validate_destination=False)

        had_existing = out.exists()
        if had_existing:
            out.replace(backup)
        try:
            staged.replace(out)
        except Exception:
            if had_existing and backup.exists():
                backup.replace(out)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return _read_result_dir(out, selected_source, validate_destination=True)
