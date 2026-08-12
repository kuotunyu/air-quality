"""Annual readiness first proves the reviewed panel before scanning its rows."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import duckdb
import polars as pl
import pytest

from twair.analysis import micro_sensor_annual_readiness
from twair.analysis.micro_sensor_annual_readiness import (
    ANNUAL_DEVICE_DAY_SCHEMA,
    AnnualMicroSensorAuditConfig,
    AnnualMicroSensorPanelConfig,
    AnnualMicroSensorReadinessResult,
    aggregate_micro_sensor_day,
    load_annual_device_day_checkpoint,
    load_annual_micro_sensor_panel_config,
    summarize_annual_micro_sensor_cohorts,
    write_annual_device_day_checkpoint,
    write_annual_micro_sensor_readiness_result,
)
from twair.config import ConfigError
from twair.ingest.micro_sensor_observations import OBSERVATION_OUTPUT_SCHEMA
from twair.net import sha256_file
from twair.store.schema import PARTITION_SCHEMA


def _identity(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _reviewed_config() -> dict[str, object]:
    year_days = [date(2025, 1, 1) + timedelta(days=index) for index in range(365)]
    absent = {
        *year_days[25:35],
        *year_days[59:71],
        *year_days[136:155],
        year_days[191],
        year_days[234],
    }
    complete = [day for day in year_days if day not in absent]
    return {
        "schema_version": 1,
        "year": 2025,
        "catalog_generations": {
            f"2025{month:02d}": _identity(f"catalog-{month}") for month in range(1, 13)
        },
        "parsed_generations": [
            {"date": day.isoformat(), "generation_sha256": _identity(day.isoformat())}
            for day in complete
        ],
        "catalogue_absent_dates": [day.isoformat() for day in sorted(absent)],
        "analysis": {
            "threads": 1,
            "memory_limit_gb": 6,
            "minimum_active_months": [3, 6, 9, 12],
            "minimum_trio_dates": [30, 90, 180, 270],
            "minimum_trio_hours": [360, 1080, 2160, 4320],
            "distance_bands_km": [0.5, 1, 2, 5, 10],
            "extreme_ranges": {
                "pm25": [0, 1000],
                "humidity": [0, 100],
                "temperature": [-100, 100],
            },
        },
    }


def test_the_annual_panel_is_an_exact_322_day_and_43_gap_calendar_partition() -> None:
    loaded = load_annual_micro_sensor_panel_config(_reviewed_config())

    assert isinstance(loaded, AnnualMicroSensorPanelConfig)
    assert loaded.year == 2025
    assert len(loaded.parsed_generations) == 322
    assert len(loaded.catalogue_absent_dates) == 43
    assert len(loaded.catalog_generations) == 12
    assert set(loaded.parsed_dates).isdisjoint(loaded.catalogue_absent_dates)
    assert set(loaded.parsed_dates) | set(loaded.catalogue_absent_dates) == {
        date(2025, 1, 1) + timedelta(days=index) for index in range(365)
    }

    shipped = load_annual_micro_sensor_panel_config()
    shipped_generations = {
        record.date: record.generation_sha256 for record in shipped.parsed_generations
    }
    assert len(shipped.parsed_generations) == 322
    assert len(shipped.catalogue_absent_dates) == 43
    assert shipped_generations[date(2025, 9, 10)] == (
        "12e369cc33236084f8dd741fd599917956843e4cc31ad56823515b6feaa7578e"
    )
    assert shipped_generations[date(2025, 11, 27)] == (
        "283854c4147efcea9f15dd869504f5939c0fb22ed24a9899b855d7859cb987d2"
    )


def test_the_annual_panel_rejects_duplicate_unsorted_or_unbound_generation_dates() -> None:
    raw = _reviewed_config()
    records = cast(list[dict[str, str]], raw["parsed_generations"])

    with pytest.raises(ConfigError, match="unique and sorted"):
        load_annual_micro_sensor_panel_config(
            {**raw, "parsed_generations": [records[1], records[0], *records[2:]]}
        )
    with pytest.raises(ConfigError, match="unique and sorted"):
        load_annual_micro_sensor_panel_config(
            {**raw, "parsed_generations": [records[0], records[0], *records[2:]]}
        )
    absent = cast(list[str], raw["catalogue_absent_dates"])
    with pytest.raises(ConfigError, match="calendar partition"):
        load_annual_micro_sensor_panel_config(
            {
                **raw,
                "catalogue_absent_dates": sorted([records[0]["date"], *absent[1:]]),
            }
        )


def test_the_annual_panel_rejects_unknown_fields_and_changed_catalogue_months() -> None:
    raw = _reviewed_config()

    with pytest.raises(ConfigError, match="unknown field"):
        load_annual_micro_sensor_panel_config({**raw, "surprise": True})
    with pytest.raises(ConfigError, match="twelve 2025 months"):
        load_annual_micro_sensor_panel_config(
            {
                **raw,
                "catalog_generations": {
                    key: value
                    for key, value in cast(dict[str, str], raw["catalog_generations"]).items()
                    if key != "202512"
                },
            }
        )
    with pytest.raises(ConfigError, match="must be 2025"):
        load_annual_micro_sensor_panel_config({**raw, "year": 2024})


def test_the_annual_audit_resource_and_cohort_grids_are_strict_design_parameters() -> None:
    raw = _reviewed_config()
    loaded = load_annual_micro_sensor_panel_config(raw)

    assert loaded.analysis == AnnualMicroSensorAuditConfig(
        threads=1,
        memory_limit_gb=6,
        minimum_active_months=(3, 6, 9, 12),
        minimum_trio_dates=(30, 90, 180, 270),
        minimum_trio_hours=(360, 1080, 2160, 4320),
        distance_bands_km=(0.5, 1.0, 2.0, 5.0, 10.0),
        extreme_ranges=(
            ("humidity", (0.0, 100.0)),
            ("pm25", (0.0, 1000.0)),
            ("temperature", (-100.0, 100.0)),
        ),
    )
    changed = dict(cast(dict[str, object], raw["analysis"]))
    changed["threads"] = 2
    with pytest.raises(ConfigError, match="threads must be one"):
        load_annual_micro_sensor_panel_config({**raw, "analysis": changed})


def test_scientific_extreme_ranges_are_reviewed_config_not_python_defaults() -> None:
    raw = _reviewed_config()
    loaded = load_annual_micro_sensor_panel_config(raw)

    assert dict(loaded.analysis.extreme_ranges) == {
        "humidity": (0.0, 100.0),
        "pm25": (0.0, 1000.0),
        "temperature": (-100.0, 100.0),
    }
    changed_analysis = dict(cast(dict[str, object], raw["analysis"]))
    changed_ranges = dict(cast(dict[str, list[int]], changed_analysis["extreme_ranges"]))
    changed_ranges["temperature"] = [100, -100]
    changed_analysis["extreme_ranges"] = changed_ranges
    with pytest.raises(ConfigError, match=r"extreme_ranges\.temperature must increase"):
        load_annual_micro_sensor_panel_config({**raw, "analysis": changed_analysis})


def _observation_frame(
    variable: str,
    rows: list[
        tuple[int, str, datetime | None, float | None, float | None, float | None, bool | None]
    ],
) -> pl.DataFrame:
    schema = dict(OBSERVATION_OUTPUT_SCHEMA)
    return pl.DataFrame(
        {
            "source_row_number": [row[0] for row in rows],
            "device_id": [row[1] for row in rows],
            "variable": [variable] * len(rows),
            "ts_local": [row[2] for row in rows],
            "value": [row[3] for row in rows],
            "lon": [row[4] for row in rows],
            "lat": [row[5] for row in rows],
            "coordinate_wgs84_valid": [row[6] for row in rows],
        },
        schema=schema,
    )


def _day_members(tmp_path: Path) -> dict[str, Path]:
    observed_day = date(2025, 1, 1)
    midnight = datetime.combine(observed_day, datetime.min.time())
    frames = {
        "pm25": _observation_frame(
            "pm25",
            [
                (1, "device-a", midnight, 10.0, 120.5, 23.5, True),
                (2, "device-a", midnight, 12.0, 120.5, 23.5, True),
                (3, "device-a", midnight + timedelta(hours=1), None, None, None, None),
                (4, "device-a", midnight + timedelta(hours=2), 1001.0, 999.0, 23.5, False),
                (5, "device-b", midnight, 8.0, 121.0, 24.0, True),
            ],
        ),
        "humidity": _observation_frame(
            "humidity",
            [
                (1, "device-a", midnight, 60.0, 120.5, 23.5, True),
                (2, "device-a", midnight + timedelta(hours=2), 101.0, 120.5, 23.5, True),
            ],
        ),
        "temperature": _observation_frame(
            "temperature",
            [
                (1, "device-a", midnight, 25.0, 120.5, 23.5, True),
                (2, "device-a", midnight + timedelta(hours=1), None, 120.5, 23.5, True),
                (3, "device-a", midnight + timedelta(hours=2), 30.0, 120.5, 23.5, True),
            ],
        ),
    }
    paths: dict[str, Path] = {}
    for variable, frame in frames.items():
        path = tmp_path / f"{variable}.parquet"
        frame.write_parquet(path)
        paths[variable] = path
    return paths


def test_one_day_aggregation_counts_null_duplicates_extremes_and_trio_hours_without_repair(
    tmp_path: Path,
) -> None:
    members = _day_members(tmp_path)

    result = aggregate_micro_sensor_day(
        day=date(2025, 1, 1),
        member_paths=members,
        config=load_annual_micro_sensor_panel_config(_reviewed_config()).analysis,
        temp_dir=tmp_path / "duckdb-temp",
    )

    assert result.schema == dict(ANNUAL_DEVICE_DAY_SCHEMA)
    assert result["device_id"].to_list() == ["device-a", "device-b"]
    first = result.row(0, named=True)
    assert {
        key: first[key]
        for key in (
            "pm25_source_rows",
            "pm25_null_value_rows",
            "pm25_null_timestamp_rows",
            "pm25_distinct_timestamps",
            "pm25_observed_hours",
            "pm25_duplicate_timestamp_groups",
            "pm25_extreme_rows",
            "humidity_source_rows",
            "humidity_observed_hours",
            "humidity_extreme_rows",
            "temperature_source_rows",
            "temperature_null_value_rows",
            "temperature_observed_hours",
            "trio_observed_hours",
            "coordinate_source_rows",
            "coordinate_null_rows",
            "coordinate_invalid_rows",
            "coordinate_positions",
        )
    } == {
        "pm25_source_rows": 4,
        "pm25_null_value_rows": 1,
        "pm25_null_timestamp_rows": 0,
        "pm25_distinct_timestamps": 3,
        "pm25_observed_hours": 2,
        "pm25_duplicate_timestamp_groups": 1,
        "pm25_extreme_rows": 1,
        "humidity_source_rows": 2,
        "humidity_observed_hours": 2,
        "humidity_extreme_rows": 1,
        "temperature_source_rows": 3,
        "temperature_null_value_rows": 1,
        "temperature_observed_hours": 2,
        "trio_observed_hours": 2,
        "coordinate_source_rows": 4,
        "coordinate_null_rows": 1,
        "coordinate_invalid_rows": 1,
        "coordinate_positions": 1,
    }
    assert (first["lon_min"], first["lon_max"], first["lat_min"], first["lat_max"]) == (
        120.5,
        120.5,
        23.5,
        23.5,
    )
    second = result.row(1, named=True)
    assert second["pm25_source_rows"] == 1
    assert second["humidity_source_rows"] == 0
    assert second["temperature_source_rows"] == 0
    assert second["trio_observed_hours"] == 0
    assert sum(result["pm25_source_rows"]) == 5
    assert sum(result["humidity_source_rows"]) == 2
    assert sum(result["temperature_source_rows"]) == 3
    assert not (tmp_path / "duckdb-temp").exists()


def test_one_day_aggregation_rejects_missing_changed_or_mislabeled_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = _day_members(tmp_path)
    config = load_annual_micro_sensor_panel_config(_reviewed_config()).analysis

    with pytest.raises(RuntimeError, match="all three parsed variables"):
        aggregate_micro_sensor_day(
            day=date(2025, 1, 1),
            member_paths={key: value for key, value in members.items() if key != "humidity"},
            config=config,
            temp_dir=tmp_path / "missing-temp",
        )

    mislabeled = pl.read_parquet(members["humidity"]).with_columns(pl.lit("pm25").alias("variable"))
    mislabeled.write_parquet(members["humidity"])
    with pytest.raises(RuntimeError, match="variable identity"):
        aggregate_micro_sensor_day(
            day=date(2025, 1, 1),
            member_paths=members,
            config=config,
            temp_dir=tmp_path / "mislabeled-temp",
        )

    members = _day_members(tmp_path)
    original_identity = micro_sensor_annual_readiness._file_identity
    calls = 0

    def changed_after_read(path: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        identity = original_identity(path)
        if calls > 3 and path == members["temperature"]:
            return {**identity, "sha256": "f" * 64}
        return identity

    monkeypatch.setattr(micro_sensor_annual_readiness, "_file_identity", changed_after_read)
    with pytest.raises(RuntimeError, match="changed while it was read"):
        aggregate_micro_sensor_day(
            day=date(2025, 1, 1),
            member_paths=members,
            config=config,
            temp_dir=tmp_path / "changed-temp",
        )


def test_one_day_aggregation_rejects_schema_and_date_drift(tmp_path: Path) -> None:
    members = _day_members(tmp_path)
    config = load_annual_micro_sensor_panel_config(_reviewed_config()).analysis
    wrong_schema = pl.read_parquet(members["pm25"]).with_columns(
        pl.col("source_row_number").cast(pl.Int64)
    )
    wrong_schema.write_parquet(members["pm25"])
    with pytest.raises(RuntimeError, match="schema changed"):
        aggregate_micro_sensor_day(
            day=date(2025, 1, 1),
            member_paths=members,
            config=config,
            temp_dir=tmp_path / "schema-temp",
        )

    members = _day_members(tmp_path)
    outside = pl.read_parquet(members["temperature"]).with_columns(
        pl.when(pl.col("source_row_number") == 1)
        .then(pl.lit(datetime(2025, 1, 2)))
        .otherwise(pl.col("ts_local"))
        .alias("ts_local")
    )
    outside.write_parquet(members["temperature"])
    with pytest.raises(RuntimeError, match="timestamp outside"):
        aggregate_micro_sensor_day(
            day=date(2025, 1, 1),
            member_paths=members,
            config=config,
            temp_dir=tmp_path / "date-temp",
        )


def test_one_day_context_distinguishes_eligible_withheld_and_absent_ground_hours(
    tmp_path: Path,
) -> None:
    midnight = datetime(2025, 1, 1)
    members: dict[str, Path] = {}
    for variable, values in {
        "pm25": (10.0, 11.0, 12.0, 13.0),
        "humidity": (60.0, 61.0, 62.0, 63.0),
        "temperature": (25.0, 26.0, 27.0, 28.0),
    }.items():
        frame = _observation_frame(
            variable,
            [
                (index + 1, "device-c", midnight + timedelta(hours=index), value, 120.5, 23.5, True)
                for index, value in enumerate(values)
            ],
        )
        path = tmp_path / f"context-{variable}.parquet"
        frame.write_parquet(path)
        members[variable] = path
    geography = pl.DataFrame(
        {
            "station_name": ["參考站"],
            "lon": [120.5],
            "lat": [23.5],
        },
        schema={"station_name": pl.String, "lon": pl.Float64, "lat": pl.Float64},
    )
    ground = pl.DataFrame(
        {
            "station_name": ["參考站", "參考站", "參考站"],
            "pollutant": ["PM2.5", "PM2.5", "PM2.5"],
            "ts_local": [
                midnight,
                midnight + timedelta(hours=1),
                midnight + timedelta(hours=2),
            ],
            "value": [15.0, None, 16.0],
            "flag": ["valid", "rain_present", None],
            "value_retained": [True, False, True],
            "imputed": [False, False, False],
            "impute_method": [None, None, None],
            "generation": ["fixture", "fixture", "fixture"],
            "source_member": ["fixture.csv", "fixture.csv", "fixture.csv"],
        },
        schema=PARTITION_SCHEMA,
    )
    ground_path = tmp_path / "ground.parquet"
    ground.write_parquet(ground_path)

    result = aggregate_micro_sensor_day(
        day=date(2025, 1, 1),
        member_paths=members,
        geography=geography,
        ground_path=ground_path,
        config=load_annual_micro_sensor_panel_config(_reviewed_config()).analysis,
        temp_dir=tmp_path / "context-temp",
    )

    row = result.row(0, named=True)
    assert row["spatial_state"] == "eligible"
    assert row["station_name"] == "參考站"
    assert row["distance_km"] == pytest.approx(0.0)
    assert row["trio_observed_hours"] == 4
    assert row["ground_present_trio_hours"] == 3
    assert row["ground_eligible_trio_hours"] == 1
    assert row["ground_present_ineligible_trio_hours"] == 2
    assert row["ground_absent_trio_hours"] == 1
    assert row["ground_present_trio_hours"] == (
        row["ground_eligible_trio_hours"] + row["ground_present_ineligible_trio_hours"]
    )
    assert row["trio_observed_hours"] == (
        row["ground_present_trio_hours"] + row["ground_absent_trio_hours"]
    )


def _device_day_row(
    *,
    observed_day: date,
    device_id: str,
    trio_hours: int,
    lon: float | None = 120.5,
    lat: float | None = 23.5,
    spatial_state: str = "eligible",
    distance_km: float | None = 1.0,
) -> dict[str, object]:
    row: dict[str, object] = {
        name: (0 if dtype == pl.Int64 else None) for name, dtype in ANNUAL_DEVICE_DAY_SCHEMA
    }
    row.update(
        {
            "date": observed_day,
            "device_id": device_id,
            "pm25_source_rows": trio_hours,
            "humidity_source_rows": trio_hours,
            "temperature_source_rows": trio_hours,
            "pm25_observed_hours": trio_hours,
            "humidity_observed_hours": trio_hours,
            "temperature_observed_hours": trio_hours,
            "trio_observed_hours": trio_hours,
            "coordinate_source_rows": trio_hours,
            "coordinate_positions": 0 if lon is None else 1,
            "lon_min": lon,
            "lon_max": lon,
            "lat_min": lat,
            "lat_max": lat,
            "spatial_state": spatial_state,
            "station_name": None if spatial_state != "eligible" else "參考站",
            "distance_km": distance_km,
            "ground_present_trio_hours": max(trio_hours - 2, 0),
            "ground_eligible_trio_hours": max(trio_hours - 3, 0),
            "ground_present_ineligible_trio_hours": 1 if trio_hours >= 2 else 0,
            "ground_absent_trio_hours": min(trio_hours, 2),
        }
    )
    if spatial_state == "invalid_or_null_coordinate":
        row["coordinate_null_rows"] = 1
    return row


def _device_day_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=dict(ANNUAL_DEVICE_DAY_SCHEMA))


def test_a_complete_day_with_no_observed_devices_persists_and_reloads_an_empty_checkpoint(
    tmp_path: Path,
) -> None:
    frame = _device_day_frame([])

    written = write_annual_device_day_checkpoint(
        frame,
        day=date(2025, 10, 31),
        parsed_generation_sha256="a" * 64,
        input_files=(),
        panel_sha256="b" * 64,
        checkpoint_root=tmp_path,
    )
    loaded = load_annual_device_day_checkpoint(
        day=date(2025, 10, 31),
        parsed_generation_sha256="a" * 64,
        input_files=(),
        panel_sha256="b" * 64,
        checkpoint_root=tmp_path,
    )

    assert written == tmp_path / "2025-10-31" / "device_days.parquet"
    assert loaded.equals(frame)
    assert loaded.height == 0


def test_daily_checkpoints_are_immutable_reusable_and_reject_tampering(tmp_path: Path) -> None:
    frame = _device_day_frame(
        [
            _device_day_row(
                observed_day=date(2025, 1, 1),
                device_id="device-a",
                trio_hours=12,
            )
        ]
    )
    inputs = (
        {"path": "pm25.parquet", "bytes": 100, "sha256": "b" * 64},
        {"path": "humidity.parquet", "bytes": 100, "sha256": "c" * 64},
        {"path": "temperature.parquet", "bytes": 100, "sha256": "d" * 64},
        {"path": "ground.parquet", "bytes": 100, "sha256": "e" * 64},
    )

    written = write_annual_device_day_checkpoint(
        frame,
        day=date(2025, 1, 1),
        parsed_generation_sha256="a" * 64,
        input_files=inputs,
        panel_sha256="f" * 64,
        checkpoint_root=tmp_path,
    )
    loaded = load_annual_device_day_checkpoint(
        day=date(2025, 1, 1),
        parsed_generation_sha256="a" * 64,
        input_files=inputs,
        panel_sha256="f" * 64,
        checkpoint_root=tmp_path,
    )

    assert loaded.equals(frame)
    assert written == tmp_path / "2025-01-01" / "device_days.parquet"
    assert (
        write_annual_device_day_checkpoint(
            frame,
            day=date(2025, 1, 1),
            parsed_generation_sha256="a" * 64,
            input_files=inputs,
            panel_sha256="f" * 64,
            checkpoint_root=tmp_path,
        )
        == written
    )
    evidence = micro_sensor_annual_readiness._annual_checkpoint_evidence(
        written.parent,
        day=date(2025, 1, 1),
    )
    assert evidence == {
        "date": "2025-01-01",
        "rows": 1,
        "manifest_bytes": (written.parent / "manifest.json").stat().st_size,
        "manifest_sha256": sha256_file(written.parent / "manifest.json"),
        "member_bytes": written.stat().st_size,
        "member_sha256": sha256_file(written),
    }
    written.write_bytes(written.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match="checksum changed"):
        load_annual_device_day_checkpoint(
            day=date(2025, 1, 1),
            parsed_generation_sha256="a" * 64,
            input_files=inputs,
            panel_sha256="f" * 64,
            checkpoint_root=tmp_path,
        )


def test_an_interrupted_daily_checkpoint_publish_leaves_no_partial_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _device_day_frame(
        [_device_day_row(observed_day=date(2025, 1, 2), device_id="device-a", trio_hours=1)]
    )
    original_replace = Path.replace

    def interrupt_staging_publish(self: Path, target: Path) -> Path:
        if self.name.startswith(".2025-01-02.staging-"):
            raise KeyboardInterrupt
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", interrupt_staging_publish)
    with pytest.raises(KeyboardInterrupt):
        write_annual_device_day_checkpoint(
            frame,
            day=date(2025, 1, 2),
            parsed_generation_sha256="a" * 64,
            input_files=(),
            panel_sha256="f" * 64,
            checkpoint_root=tmp_path,
        )

    assert not (tmp_path / "2025-01-02").exists()
    assert list(tmp_path.glob(".2025-01-02.staging-*")) == []


def test_annual_cohorts_keep_sparse_invalid_and_moving_devices_out_of_thresholds() -> None:
    parsed_dates = load_annual_micro_sensor_panel_config().parsed_dates
    rows = [
        _device_day_row(
            observed_day=observed_day,
            device_id="eligible",
            trio_hours=16,
        )
        for observed_day in parsed_dates[:270]
    ]
    rows.extend(
        _device_day_row(observed_day=observed_day, device_id="sparse", trio_hours=12)
        for observed_day in parsed_dates[:29]
    )
    rows.extend(
        _device_day_row(
            observed_day=observed_day,
            device_id="moving",
            trio_hours=12,
            lon=120.5 if index < 15 else 120.6,
        )
        for index, observed_day in enumerate(parsed_dates[:30])
    )
    rows.append(
        _device_day_row(
            observed_day=parsed_dates[0],
            device_id="invalid",
            trio_hours=12,
            lon=None,
            lat=None,
            spatial_state="invalid_or_null_coordinate",
            distance_km=None,
        )
    )

    cohorts, thresholds, exclusions = summarize_annual_micro_sensor_cohorts(
        _device_day_frame(rows),
        config=load_annual_micro_sensor_panel_config().analysis,
    )

    by_device = {row["device_id"]: row for row in cohorts.iter_rows(named=True)}
    assert by_device["eligible"]["active_dates"] == 270
    assert by_device["eligible"]["trio_observed_hours"] == 4320
    assert by_device["eligible"]["spatial_state"] == "eligible"
    assert by_device["moving"]["spatial_state"] == "moving_coordinate"
    assert by_device["invalid"]["spatial_state"] == "invalid_or_null_coordinate"
    strict = thresholds.filter(
        (pl.col("minimum_active_months") == 9)
        & (pl.col("minimum_trio_dates") == 270)
        & (pl.col("minimum_trio_hours") == 4320)
        & (pl.col("distance_km") == 1.0)
    ).row(0, named=True)
    assert strict["devices"] == 1
    assert dict(exclusions.select("reason", "devices").iter_rows()) == {
        "eligible_at_relaxed_threshold": 1,
        "fewer_than_3_active_months": 1,
        "invalid_or_null_coordinate": 1,
        "moving_coordinate": 1,
    }


def test_annual_reduction_scans_checkpoints_without_a_polars_whole_year_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = []
    for index in range(2):
        path = tmp_path / f"day-{index}.parquet"
        _device_day_frame(
            [
                _device_day_row(
                    observed_day=date(2025, 1, index + 1),
                    device_id="device-a",
                    trio_hours=12,
                )
            ]
        ).write_parquet(path)
        paths.append(path)

    def reject_polars_collection(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("annual checkpoints were collected through Polars")

    monkeypatch.setattr(pl, "read_parquet", reject_polars_collection)
    monkeypatch.setattr(pl, "concat", reject_polars_collection)

    cohorts, thresholds, exclusions, rows = (
        micro_sensor_annual_readiness._summarize_annual_micro_sensor_checkpoint_paths(
            tuple(paths),
            config=load_annual_micro_sensor_panel_config(_reviewed_config()).analysis,
            temp_dir=tmp_path / "annual-duckdb",
        )
    )

    assert rows == 2
    assert cohorts.row(0, named=True)["active_dates"] == 2
    assert thresholds.height == 4 * 4 * 4 * 5
    assert exclusions["devices"].sum() == 1


def test_annual_device_days_are_materialized_from_checkpoints_inside_duckdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = []
    for index in range(2):
        path = tmp_path / f"member-{index}.parquet"
        _device_day_frame(
            [
                _device_day_row(
                    observed_day=date(2025, 1, index + 1),
                    device_id="device-a",
                    trio_hours=12,
                )
            ]
        ).write_parquet(path)
        paths.append(path)

    def reject_polars_collection(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("annual checkpoints were collected through Polars")

    monkeypatch.setattr(pl, "read_parquet", reject_polars_collection)
    monkeypatch.setattr(pl, "concat", reject_polars_collection)
    destination = tmp_path / "device_days.parquet"
    micro_sensor_annual_readiness._write_annual_device_days_from_checkpoints(
        tuple(paths),
        destination=destination,
        config=load_annual_micro_sensor_panel_config(_reviewed_config()).analysis,
        temp_dir=tmp_path / "materialize-duckdb",
    )

    connection = duckdb.connect()
    try:
        rows = connection.execute(
            f"SELECT count(*) FROM read_parquet('{destination.as_posix()}')"
        ).fetchone()
    finally:
        connection.close()
    assert rows == (2,)


def test_the_annual_run_lock_rejects_a_live_writer_and_is_released_by_process_exit(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".run.lock"
    holder_script = "\n".join(
        (
            "import os",
            "import sys",
            "from pathlib import Path",
            "from twair.analysis.micro_sensor_annual_readiness import _exclusive_run_lock",
            "with _exclusive_run_lock(Path(sys.argv[1])):",
            "    print('locked', flush=True)",
            "    sys.stdin.read(1)",
            "    os._exit(0)",
        )
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"
    try:
        with (
            pytest.raises(RuntimeError, match="another annual"),
            micro_sensor_annual_readiness._exclusive_run_lock(lock),
        ):
            raise AssertionError("a second writer entered the annual lock")
    finally:
        _, stderr = holder.communicate("exit", timeout=10)

    assert holder.returncode == 0, stderr
    assert lock.is_file()
    with micro_sensor_annual_readiness._exclusive_run_lock(lock):
        assert lock.is_file()
    assert lock.is_file()


def test_the_annual_run_lock_rejects_nested_same_process_ownership(tmp_path: Path) -> None:
    lock = tmp_path / ".run.lock"

    with (
        micro_sensor_annual_readiness._exclusive_run_lock(lock),
        pytest.raises(RuntimeError, match="another annual"),
        micro_sensor_annual_readiness._exclusive_run_lock(lock),
    ):
        raise AssertionError("nested same-process ownership was admitted")


def test_the_annual_run_lock_is_released_when_the_protected_body_raises(tmp_path: Path) -> None:
    lock = tmp_path / ".run.lock"

    with (
        pytest.raises(ValueError, match="protected body failed"),
        micro_sensor_annual_readiness._exclusive_run_lock(lock),
    ):
        raise ValueError("protected body failed")

    with micro_sensor_annual_readiness._exclusive_run_lock(lock):
        assert lock.is_file()


def test_one_advisory_lock_covers_annual_preparation_and_final_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = load_annual_micro_sensor_panel_config(_reviewed_config())
    events: list[str] = []
    lock_held = False
    sentinel = AnnualMicroSensorReadinessResult(
        calendar_coverage=pl.DataFrame(),
        device_days=pl.DataFrame(),
        device_cohorts=pl.DataFrame(),
        cohort_thresholds=pl.DataFrame(),
        exclusions=pl.DataFrame(),
        summary={},
        manifest={"generation_sha256": "provisional"},
    )
    written = {"manifest": tmp_path / "manifest.json"}
    original_read_json = micro_sensor_annual_readiness._read_json

    @contextmanager
    def fake_lock(_path: Path) -> Iterator[None]:
        nonlocal lock_held
        lock_held = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            lock_held = False

    def fake_run(**kwargs: object) -> AnnualMicroSensorReadinessResult:
        assert lock_held
        assert kwargs["_lock_held"] is True
        events.append("prepare")
        return sentinel

    def fake_write(*_args: object, **_kwargs: object) -> dict[str, Path]:
        assert lock_held
        events.append("publish")
        micro_sensor_annual_readiness._write_json(
            written["manifest"],
            {"generation_sha256": "final"},
        )
        return written

    def observe_manifest_load(path: Path, *, label: str) -> dict[str, object]:
        assert lock_held
        events.append("manifest-load")
        return original_read_json(path, label=label)

    monkeypatch.setattr(micro_sensor_annual_readiness, "configured_data_root", lambda: tmp_path)
    monkeypatch.setattr(micro_sensor_annual_readiness, "_exclusive_run_lock", fake_lock)
    monkeypatch.setattr(
        micro_sensor_annual_readiness,
        "_prepare_annual_micro_sensor_readiness",
        fake_run,
    )
    monkeypatch.setattr(
        micro_sensor_annual_readiness,
        "write_annual_micro_sensor_readiness_result",
        fake_write,
    )
    monkeypatch.setattr(
        micro_sensor_annual_readiness,
        "_read_json",
        observe_manifest_load,
    )

    result = micro_sensor_annual_readiness.run_and_write_annual_micro_sensor_readiness(
        data_root=tmp_path,
        panel=panel,
        destination=tmp_path / "published",
    )

    returned, returned_paths = result
    assert returned_paths == written
    assert returned.manifest == {"generation_sha256": "final"}
    assert events == ["lock-enter", "prepare", "publish", "manifest-load", "lock-exit"]


def _annual_manifest(rows: dict[str, int]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "analysis": "annual_micro_sensor_readiness",
        "config": {"threads": 1},
        "inputs": {"panel_sha256": "b" * 64},
        "checkpoint_inventory": [
            {
                "date": "2025-01-01",
                "rows": rows["device_days"],
                "manifest_bytes": 100,
                "manifest_sha256": "c" * 64,
                "member_bytes": 200,
                "member_sha256": "d" * 64,
            }
        ],
        "checkpoint_run": [{"date": "2025-01-01", "state": "created"}],
        "output_rows": rows,
        "claim_boundary": dict(micro_sensor_annual_readiness._CLAIM_BOUNDARY),
        "generation_sha256": "a" * 64,
    }


def test_the_final_annual_result_is_published_atomically_with_all_measured_members(
    tmp_path: Path,
) -> None:
    panel = load_annual_micro_sensor_panel_config()
    device_days = _device_day_frame(
        [_device_day_row(observed_day=panel.parsed_dates[0], device_id="device-a", trio_hours=12)]
    )
    cohorts, thresholds, exclusions = summarize_annual_micro_sensor_cohorts(
        device_days,
        config=panel.analysis,
    )
    calendar = micro_sensor_annual_readiness._calendar_coverage(panel)
    frames = {
        "calendar_coverage": calendar,
        "device_days": device_days,
        "device_cohorts": cohorts,
        "cohort_thresholds": thresholds,
        "exclusions": exclusions,
    }
    rows = {name: frame.height for name, frame in frames.items()}
    result = AnnualMicroSensorReadinessResult(
        **frames,
        summary={"output_rows": rows},
        manifest=_annual_manifest(rows),
    )

    written = write_annual_micro_sensor_readiness_result(
        result,
        destination=tmp_path / "published",
    )

    assert set(written) == {*frames, "summary", "manifest"}
    assert all(path.is_file() for path in written.values())
    manifest = micro_sensor_annual_readiness._read_json(written["manifest"], label="test manifest")
    assert set(manifest["members"]) == set(frames)
    assert manifest["checkpoint_inventory"] == result.manifest["checkpoint_inventory"]
    assert manifest["checkpoint_run"] == result.manifest["checkpoint_run"]
    assert manifest["generation_sha256"] != "a" * 64
    assert manifest["generation_sha256"] == micro_sensor_annual_readiness._hash_value(
        micro_sensor_annual_readiness._final_generation_identity(manifest)
    )
    with pytest.raises(RuntimeError, match="already exists"):
        write_annual_micro_sensor_readiness_result(
            result,
            destination=tmp_path / "published",
        )


def test_the_final_writer_accepts_path_backed_device_days_without_collecting_them(
    tmp_path: Path,
) -> None:
    panel = load_annual_micro_sensor_panel_config(_reviewed_config())
    device_days = _device_day_frame(
        [
            _device_day_row(
                observed_day=panel.parsed_dates[0],
                device_id="device-a",
                trio_hours=12,
            )
        ]
    )
    checkpoint = tmp_path / "checkpoint.parquet"
    device_days.write_parquet(checkpoint)
    cohorts, thresholds, exclusions = summarize_annual_micro_sensor_cohorts(
        device_days,
        config=panel.analysis,
    )
    frames = {
        "calendar_coverage": micro_sensor_annual_readiness._calendar_coverage(panel),
        "device_cohorts": cohorts,
        "cohort_thresholds": thresholds,
        "exclusions": exclusions,
    }
    rows = {name: frame.height for name, frame in frames.items()}
    rows["device_days"] = device_days.height
    result = AnnualMicroSensorReadinessResult(
        **frames,
        device_days=(checkpoint,),
        summary={"output_rows": rows},
        manifest=_annual_manifest(rows),
    )

    written = write_annual_micro_sensor_readiness_result(
        result,
        destination=tmp_path / "path-backed",
        config=panel.analysis,
    )

    assert written["device_days"].is_file()
    assert pl.scan_parquet(written["device_days"]).select(pl.len()).collect().item() == 1


def test_the_orchestrator_returns_the_exact_persisted_generation_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = load_annual_micro_sensor_panel_config(_reviewed_config())
    device_days = _device_day_frame(
        [
            _device_day_row(
                observed_day=panel.parsed_dates[0],
                device_id="device-a",
                trio_hours=12,
            )
        ]
    )
    cohorts, thresholds, exclusions = summarize_annual_micro_sensor_cohorts(
        device_days,
        config=panel.analysis,
    )
    frames = {
        "calendar_coverage": micro_sensor_annual_readiness._calendar_coverage(panel),
        "device_days": device_days,
        "device_cohorts": cohorts,
        "cohort_thresholds": thresholds,
        "exclusions": exclusions,
    }
    rows = {name: frame.height for name, frame in frames.items()}
    provisional = AnnualMicroSensorReadinessResult(
        **frames,
        summary={"output_rows": rows},
        manifest=_annual_manifest(rows),
    )
    monkeypatch.setattr(micro_sensor_annual_readiness, "configured_data_root", lambda: tmp_path)
    monkeypatch.setattr(
        micro_sensor_annual_readiness,
        "outputs_dir",
        lambda name: tmp_path / "outputs" / name,
    )
    monkeypatch.setattr(
        micro_sensor_annual_readiness,
        "_prepare_annual_micro_sensor_readiness",
        lambda **_kwargs: provisional,
    )

    returned, written = micro_sensor_annual_readiness.run_and_write_annual_micro_sensor_readiness(
        data_root=tmp_path,
        panel=panel,
    )

    persisted = micro_sensor_annual_readiness._read_json(
        written["manifest"],
        label="persisted annual manifest",
    )
    assert returned.manifest == persisted
    assert returned.manifest["generation_sha256"] == written["manifest"].parent.name
