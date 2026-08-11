"""Parse one verified micro-sensor day without repairing its observations."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from twair.config import ConfigError, load_conf
from twair.ingest.micro_sensors import (
    MicroSensorSource,
    load_micro_sensor_source,
    load_observation_generation,
)
from twair.net import sha256_file
from twair.paths import interim_dir
from twair.provenance import git_state

OBSERVATION_OUTPUT_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("source_row_number", pl.UInt64),
    ("device_id", pl.String),
    ("variable", pl.String),
    ("ts_local", pl.Datetime("us")),
    ("value", pl.Float64),
    ("lon", pl.Float64),
    ("lat", pl.Float64),
    ("coordinate_wgs84_valid", pl.Boolean),
)


@dataclass(frozen=True, slots=True)
class MicroSensorParserContract:
    timestamp_format: str
    value_columns: dict[str, str]


@dataclass(frozen=True, slots=True)
class ParsedObservationSummary:
    rows: int
    unique_devices: int
    null_counts: dict[str, int]
    min_time: str | None
    max_time: str | None
    min_value: float | None
    max_value: float | None
    duplicate_key_groups: int
    rows_in_duplicate_keys: int
    largest_duplicate_key_group: int | None
    coordinate_wgs84_invalid_rows: int


@dataclass(frozen=True, slots=True)
class MicroSensorObservationWrite:
    generation_sha256: str
    directory: Path
    manifest: dict[str, Any]


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{label} must be a mapping with string keys")
    return value


def load_micro_sensor_parser_contract() -> MicroSensorParserContract:
    config = _mapping(load_conf("micro_sensors"), label="micro_sensors")
    source = _mapping(config.get("source"), label="micro_sensors.source")
    parser = _mapping(source.get("parser"), label="micro_sensors.source.parser")
    timestamp_format = parser.get("timestamp_format")
    if not isinstance(timestamp_format, str) or not timestamp_format:
        raise ConfigError("micro_sensors.source.parser.timestamp_format must be nonempty")
    value_columns = _mapping(
        parser.get("value_columns"),
        label="micro_sensors.source.parser.value_columns",
    )
    required = tuple(load_micro_sensor_source().required_variables)
    if set(value_columns) != set(required) or not all(
        isinstance(value_columns[variable], str) and value_columns[variable]
        for variable in required
    ):
        raise ConfigError("micro-sensor parser value columns must match required variables")
    if len(set(value_columns.values())) != len(value_columns):
        raise ConfigError("micro-sensor parser value columns must be unique")
    return MicroSensorParserContract(
        timestamp_format=timestamp_format,
        value_columns={variable: str(value_columns[variable]) for variable in required},
    )


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"micro-sensor parsed manifest is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"micro-sensor parsed manifest must be an object: {path}")
    return value


def _parse_day(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("micro-sensor parser day must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ConfigError("micro-sensor parser day must use YYYY-MM-DD")
    return parsed


def _schema_contract() -> dict[str, str]:
    return {name: str(dtype) for name, dtype in OBSERVATION_OUTPUT_SCHEMA}


def _parser_contract(contract: MicroSensorParserContract) -> dict[str, object]:
    return {
        "timestamp_format": contract.timestamp_format,
        "value_columns": contract.value_columns,
        "output_schema": _schema_contract(),
    }


def _nonempty(column: str) -> pl.Expr:
    return pl.col(column).is_not_null() & pl.col(column).str.len_bytes().gt(0)


def _validate_csv_shape(path: Path, *, expected_header: list[str], variable: str) -> None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            records = csv.reader(source)
            if next(records, None) != expected_header:
                raise ConfigError(f"micro-sensor {variable} observation header changed")
            for source_row_number, record in enumerate(records, start=1):
                if len(record) != len(expected_header):
                    raise ConfigError(
                        "micro-sensor observation row "
                        f"{source_row_number} must contain exactly five fields"
                    )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ConfigError(f"micro-sensor observation CSV is unreadable: {path}") from exc


def _source_lazy(path: Path, *, value_column: str) -> pl.LazyFrame:
    return pl.scan_csv(
        path,
        has_header=True,
        schema={
            "deviceId": pl.String,
            value_column: pl.String,
            "time": pl.String,
            "lon": pl.String,
            "lat": pl.String,
        },
        empty_string_is_null=True,
        ignore_errors=False,
        try_parse_dates=False,
        truncate_ragged_lines=False,
    )


def _typed_expressions(
    *,
    value_column: str,
    timestamp_format: str,
) -> tuple[pl.Expr, pl.Expr, pl.Expr, pl.Expr]:
    value = pl.col(value_column).cast(pl.Float64, strict=False)
    timestamp = pl.col("time").str.to_datetime(
        format=timestamp_format,
        time_unit="us",
        strict=False,
    )
    lon = pl.col("lon").cast(pl.Float64, strict=False)
    lat = pl.col("lat").cast(pl.Float64, strict=False)
    return value, timestamp, lon, lat


def _validate_source_values(
    source: pl.LazyFrame,
    *,
    value_column: str,
    contract: MicroSensorParserContract,
    day: date,
) -> None:
    value, timestamp, lon, lat = _typed_expressions(
        value_column=value_column,
        timestamp_format=contract.timestamp_format,
    )
    next_day = day + timedelta(days=1)
    counts = (
        source.select(
            (_nonempty(value_column) & value.is_null()).sum().alias("value"),
            (_nonempty("time") & timestamp.is_null()).sum().alias("time"),
            (_nonempty("lon") & lon.is_null()).sum().alias("longitude"),
            (_nonempty("lat") & lat.is_null()).sum().alias("latitude"),
            (value.is_not_null() & value.is_finite().not_()).sum().alias("nonfinite_value"),
            (lon.is_not_null() & lon.is_finite().not_()).sum().alias("nonfinite_lon"),
            (lat.is_not_null() & lat.is_finite().not_()).sum().alias("nonfinite_lat"),
            (
                timestamp.is_not_null()
                & ((timestamp < pl.lit(day)) | (timestamp >= pl.lit(next_day)))
            )
            .sum()
            .alias("outside_day"),
        )
        .collect(engine="streaming")
        .row(0, named=True)
    )
    for field in ("value", "time", "longitude", "latitude"):
        if counts[field]:
            raise ConfigError(f"micro-sensor observation has a nonempty invalid {field}")
    if counts["nonfinite_value"] or counts["nonfinite_lon"] or counts["nonfinite_lat"]:
        raise ConfigError("micro-sensor observation has a non-finite numeric field")
    if counts["outside_day"]:
        raise ConfigError(f"micro-sensor observation has a timestamp outside {day.isoformat()}")


def _summary_from_parquet(path: Path) -> ParsedObservationSummary:
    source = pl.scan_parquet(path)
    row = (
        source.select(
            pl.len().alias("rows"),
            pl.col("device_id").n_unique().alias("unique_devices"),
            pl.col("ts_local").min().alias("min_time"),
            pl.col("ts_local").max().alias("max_time"),
            pl.col("value").min().alias("min_value"),
            pl.col("value").max().alias("max_value"),
            pl.col("coordinate_wgs84_valid").eq(False).sum().alias("invalid_coordinates"),
            *(
                pl.col(name).null_count().alias(f"null_{name}")
                for name in (
                    "device_id",
                    "ts_local",
                    "value",
                    "lon",
                    "lat",
                )
            ),
        )
        .collect(engine="streaming")
        .row(0, named=True)
    )
    duplicate = (
        source.group_by("device_id", "ts_local")
        .len()
        .filter(pl.col("len") > 1)
        .collect(engine="streaming")
    )
    duplicate_lengths = [int(value) for value in duplicate["len"].to_list()]
    return ParsedObservationSummary(
        rows=int(row["rows"]),
        unique_devices=int(row["unique_devices"]),
        null_counts={
            name: int(row[f"null_{name}"])
            for name in ("device_id", "ts_local", "value", "lon", "lat")
        },
        min_time=None if row["min_time"] is None else row["min_time"].isoformat(sep=" "),
        max_time=None if row["max_time"] is None else row["max_time"].isoformat(sep=" "),
        min_value=None if row["min_value"] is None else float(row["min_value"]),
        max_value=None if row["max_value"] is None else float(row["max_value"]),
        duplicate_key_groups=duplicate.height,
        rows_in_duplicate_keys=sum(duplicate_lengths),
        largest_duplicate_key_group=(None if not duplicate_lengths else max(duplicate_lengths)),
        coordinate_wgs84_invalid_rows=int(row["invalid_coordinates"]),
    )


def parse_observation_csv(
    source_path: Path,
    destination: Path,
    *,
    variable: str,
    day: str,
    contract: MicroSensorParserContract | None = None,
) -> ParsedObservationSummary:
    """Parse one reviewed CSV without dropping or repairing a source row."""
    selected = contract or load_micro_sensor_parser_contract()
    if variable not in selected.value_columns:
        raise ConfigError(f"unrecognised micro-sensor parser variable: {variable}")
    parsed_day = _parse_day(day)
    value_column = selected.value_columns[variable]
    if destination.exists():
        raise RuntimeError(f"micro-sensor parsed destination already exists: {destination}")
    _validate_csv_shape(
        source_path,
        expected_header=["deviceId", value_column, "time", "lon", "lat"],
        variable=variable,
    )
    part = destination.with_suffix(destination.suffix + ".part")
    part.unlink(missing_ok=True)
    try:
        source = _source_lazy(source_path, value_column=value_column)
        _validate_source_values(
            source,
            value_column=value_column,
            contract=selected,
            day=parsed_day,
        )
        value, timestamp, lon, lat = _typed_expressions(
            value_column=value_column,
            timestamp_format=selected.timestamp_format,
        )
        parsed = (
            source.with_row_index("source_row_number", offset=1)
            .with_columns(
                value.alias("value"),
                timestamp.alias("ts_local"),
                lon.alias("lon_number"),
                lat.alias("lat_number"),
            )
            .with_columns(
                pl.when(pl.col("lon_number").is_null() | pl.col("lat_number").is_null())
                .then(pl.lit(None, dtype=pl.Boolean))
                .otherwise(
                    pl.col("lon_number").is_between(-180, 180, closed="both")
                    & pl.col("lat_number").is_between(-90, 90, closed="both")
                )
                .alias("coordinate_wgs84_valid")
            )
            .select(
                pl.col("source_row_number").cast(pl.UInt64),
                pl.col("deviceId").alias("device_id"),
                pl.lit(variable, dtype=pl.String).alias("variable"),
                "ts_local",
                "value",
                pl.col("lon_number").alias("lon"),
                pl.col("lat_number").alias("lat"),
                "coordinate_wgs84_valid",
            )
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        parsed.sink_parquet(
            part,
            compression="zstd",
            maintain_order=True,
            engine="streaming",
        )
        actual_schema = pl.read_parquet_schema(part)
        if actual_schema != pl.Schema(OBSERVATION_OUTPUT_SCHEMA):
            raise RuntimeError("micro-sensor parsed Parquet schema changed")
        summary = _summary_from_parquet(part)
        part.replace(destination)
    except (pl.exceptions.PolarsError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise ConfigError(f"micro-sensor observation CSV cannot be parsed: {source_path}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return summary


def _request_identity(
    raw_manifest: dict[str, Any],
    *,
    raw_generation: str,
    contract: MicroSensorParserContract,
) -> dict[str, object]:
    parser = _parser_contract(contract)
    members = raw_manifest.get("members")
    if not isinstance(members, dict):
        raise RuntimeError("raw micro-sensor observation manifest has no members")
    return {
        "schema_version": 1,
        "raw_observation_generation_sha256": raw_generation,
        "date": raw_manifest.get("date"),
        "parser_contract": parser,
        "parser_contract_sha256": _sha256(_canonical_json(parser)),
        "raw_members": members,
    }


def _generation_identity(manifest: dict[str, Any]) -> dict[str, object]:
    return {
        "schema_version": manifest.get("schema_version"),
        "request_sha256": manifest.get("request_sha256"),
        "raw_observation_generation_sha256": manifest.get("raw_observation_generation_sha256"),
        "date": manifest.get("date"),
        "parser_contract_sha256": manifest.get("parser_contract_sha256"),
        "raw_members": manifest.get("raw_members"),
        "members": manifest.get("members"),
    }


def _validate_parsed_generation(
    directory: Path,
    *,
    request_identity: dict[str, object],
    request_sha256: str,
    contract: MicroSensorParserContract,
    require_directory_name: bool = True,
) -> dict[str, Any]:
    manifest = _manifest(directory / "manifest.json")
    if manifest.get("kind") != "interim_micro_sensor_observation_day":
        raise RuntimeError(f"micro-sensor parsed generation kind is invalid: {directory}")
    if manifest.get("request_sha256") != request_sha256:
        raise RuntimeError("micro-sensor parsed request identity changed")
    for key, expected in request_identity.items():
        if manifest.get(key) != expected:
            raise RuntimeError(f"micro-sensor parsed manifest changed: {key}")
    members = manifest.get("members")
    if not isinstance(members, dict):
        raise RuntimeError("micro-sensor parsed manifest has no member identities")
    expected_names = {f"{variable}.parquet" for variable in contract.value_columns}
    if set(members) != expected_names:
        raise RuntimeError("micro-sensor parsed member names changed")
    if {path.name for path in directory.iterdir()} != {"manifest.json", *expected_names}:
        raise RuntimeError("micro-sensor parsed generation members changed")
    for name, value in members.items():
        if not isinstance(value, dict):
            raise RuntimeError(f"micro-sensor parsed member identity is invalid: {name}")
        path = directory / name
        if value.get("bytes") != path.stat().st_size or value.get("sha256") != sha256_file(path):
            raise RuntimeError(f"micro-sensor parsed checksum changed: {path}")
        if value.get("schema") != _schema_contract():
            raise RuntimeError(f"micro-sensor parsed schema identity changed: {path}")
        summary = _summary_from_parquet(path)
        if value.get("summary") != asdict(summary):
            raise RuntimeError(f"micro-sensor parsed summary changed: {path}")
        variable = name.removesuffix(".parquet")
        frame = pl.scan_parquet(path)
        observed = frame.select(pl.col("variable").unique()).collect(engine="streaming")
        if summary.rows and observed["variable"].to_list() != [variable]:
            raise RuntimeError(f"micro-sensor parsed variable changed: {path}")
    generation = _sha256(_canonical_json(_generation_identity(manifest)))
    if manifest.get("generation_sha256") != generation or (
        require_directory_name and directory.name != generation
    ):
        raise RuntimeError("micro-sensor parsed generation identity changed")
    return manifest


def _existing_generation(
    root: Path,
    *,
    request_identity: dict[str, object],
    request_sha256: str,
    contract: MicroSensorParserContract,
) -> MicroSensorObservationWrite | None:
    if not root.exists():
        return None
    matches: list[Path] = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or re.fullmatch(r"[0-9a-f]{64}", candidate.name) is None:
            continue
        try:
            manifest = _manifest(candidate / "manifest.json")
        except RuntimeError:
            continue
        if manifest.get("request_sha256") == request_sha256:
            matches.append(candidate)
    if len(matches) > 1:
        raise RuntimeError("multiple micro-sensor parsed generations claim one request")
    if not matches:
        return None
    manifest = _validate_parsed_generation(
        matches[0],
        request_identity=request_identity,
        request_sha256=request_sha256,
        contract=contract,
    )
    return MicroSensorObservationWrite(
        generation_sha256=str(manifest["generation_sha256"]),
        directory=matches[0],
        manifest=manifest,
    )


def _extract_only_member(archive_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) != 1 or infos[0].is_dir():
                raise ConfigError("micro-sensor parser requires exactly one CSV member")
            with archive.open(infos[0]) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output, length=1 << 20)
            if destination.stat().st_size != infos[0].file_size:
                raise ConfigError("micro-sensor extracted CSV byte count changed")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ConfigError(
            f"micro-sensor observation ZIP cannot be extracted: {archive_path}"
        ) from exc


def parse_micro_sensor_observation_generation(
    raw_generation_sha256: str,
    *,
    source: MicroSensorSource | None = None,
    contract: MicroSensorParserContract | None = None,
    raw_observation_root: Path | None = None,
    raw_catalog_root: Path | None = None,
    interim_catalog_root: Path | None = None,
    interim_observation_root: Path | None = None,
    generated_at: str | None = None,
    git_sha: str | None = None,
    git_dirty: bool | None = None,
) -> MicroSensorObservationWrite:
    """Parse one raw generation into immutable per-variable Parquet members."""
    selected_source = source or load_micro_sensor_source()
    selected_contract = contract or load_micro_sensor_parser_contract()
    raw = load_observation_generation(
        raw_generation_sha256,
        source=selected_source,
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        observation_root=raw_observation_root,
    )
    request_identity = _request_identity(
        raw.manifest,
        raw_generation=raw.generation_sha256,
        contract=selected_contract,
    )
    request_sha = _sha256(_canonical_json(request_identity))
    root = interim_observation_root or interim_dir("micro_sensors") / "observations" / "generations"
    existing = _existing_generation(
        root,
        request_identity=request_identity,
        request_sha256=request_sha,
        contract=selected_contract,
    )
    if existing is not None:
        return existing

    day = raw.manifest.get("date")
    selected_archives = raw.manifest.get("selected_archives")
    if not isinstance(day, str) or not isinstance(selected_archives, list):
        raise RuntimeError("raw micro-sensor observation selection is invalid")
    records = {
        record.get("variable"): record
        for record in selected_archives
        if isinstance(record, dict) and isinstance(record.get("variable"), str)
    }
    if set(records) != set(selected_contract.value_columns):
        raise RuntimeError("raw micro-sensor observation variables changed")

    root.mkdir(parents=True, exist_ok=True)
    staged = root / f".staging-{request_sha}"
    if staged.exists():
        if not staged.is_dir():
            raise RuntimeError(f"micro-sensor parser staging path is not a directory: {staged}")
        shutil.rmtree(staged)
    staged.mkdir()
    try:
        member_records: dict[str, dict[str, object]] = {}
        for variable in selected_contract.value_columns:
            filename = records[variable].get("filename")
            if not isinstance(filename, str):
                raise RuntimeError(f"raw micro-sensor {variable} filename is invalid")
            extracted = staged / f".{variable}-source.csv"
            try:
                _extract_only_member(raw.directory / filename, extracted)
                parquet = staged / f"{variable}.parquet"
                summary = parse_observation_csv(
                    extracted,
                    parquet,
                    variable=variable,
                    day=day,
                    contract=selected_contract,
                )
            finally:
                extracted.unlink(missing_ok=True)
            member_records[parquet.name] = {
                "bytes": parquet.stat().st_size,
                "sha256": sha256_file(parquet),
                "schema": _schema_contract(),
                "summary": asdict(summary),
            }
        measured_sha, measured_dirty = git_state()
        manifest: dict[str, Any] = {
            **request_identity,
            "kind": "interim_micro_sensor_observation_day",
            "request_sha256": request_sha,
            "members": member_records,
            "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
            "git_sha": measured_sha if git_sha is None else git_sha,
            "git_dirty": measured_dirty if git_dirty is None else git_dirty,
        }
        generation = _sha256(_canonical_json(_generation_identity(manifest)))
        manifest["generation_sha256"] = generation
        _write_json(staged / "manifest.json", manifest)
        _validate_parsed_generation(
            staged,
            request_identity=request_identity,
            request_sha256=request_sha,
            contract=selected_contract,
            require_directory_name=False,
        )
        destination = root / generation
        if destination.exists():
            existing_manifest = _validate_parsed_generation(
                destination,
                request_identity=request_identity,
                request_sha256=request_sha,
                contract=selected_contract,
            )
            shutil.rmtree(staged)
            return MicroSensorObservationWrite(
                generation_sha256=generation,
                directory=destination,
                manifest=existing_manifest,
            )
        staged.replace(destination)
    except BaseException:
        if staged.exists():
            shutil.rmtree(staged)
        raise
    final_manifest = _validate_parsed_generation(
        destination,
        request_identity=request_identity,
        request_sha256=request_sha,
        contract=selected_contract,
    )
    return MicroSensorObservationWrite(
        generation_sha256=generation,
        directory=destination,
        manifest=final_manifest,
    )
