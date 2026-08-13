from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twair.analysis.micro_sensor_annual_readiness import (
    ANNUAL_CALENDAR_SCHEMA,
    ANNUAL_COHORT_THRESHOLD_SCHEMA,
    ANNUAL_DEVICE_COHORT_SCHEMA,
    ANNUAL_DEVICE_DAY_SCHEMA,
    ANNUAL_EXCLUSION_SCHEMA,
)
from twair.config import ConfigError, load_conf
from twair.ingest.micro_sensor_observations import OBSERVATION_OUTPUT_SCHEMA
from twair.net import sha256_file
from twair.store.schema import PARTITION_SCHEMA

ANNUAL_GENERATION = "c74ec40428a907e98821efbaf36c36386d2c1b99de69791b49f157eb7947e5bb"
ANNUAL_MEMBER_SCHEMAS = {
    "calendar_coverage": ANNUAL_CALENDAR_SCHEMA,
    "device_days": ANNUAL_DEVICE_DAY_SCHEMA,
    "device_cohorts": ANNUAL_DEVICE_COHORT_SCHEMA,
    "cohort_thresholds": ANNUAL_COHORT_THRESHOLD_SCHEMA,
    "exclusions": ANNUAL_EXCLUSION_SCHEMA,
}
AGREEMENT_DAY = date(2025, 1, 2)
AGREEMENT_CANDIDATES = pl.DataFrame(
    {
        "device_id": ["eligible", "ground-absent", "ground-ineligible", "device-absent"],
        "station_name": ["station-a", "station-b", "station-c", "station-d"],
        "distance_km": [0.1, 0.2, 0.3, 0.4],
    }
)


def _canonical_hash(value: object) -> str:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    return sha256(payload).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _reviewed_geography_fixture() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": ["station-a"],
            "lon": [121.5],
            "lat": [25.0],
            "geo_source": ["current_register"],
            "geo_source_record_namespace": ["aqx_p_07"],
            "geo_source_record_id": ["1"],
        }
    )


def _geography_hash(frame: pl.DataFrame) -> str:
    return _canonical_hash(
        frame.select(
            "station_name",
            "lon",
            "lat",
            "geo_source",
            "geo_source_record_namespace",
            "geo_source_record_id",
        )
        .sort("station_name")
        .to_dicts()
    )


def _empty_annual_fixture(tmp_path: Path) -> tuple[Path, str, pl.DataFrame]:
    generation = tmp_path / "annual-readiness-staging"
    generation.mkdir(parents=True)
    geography = _reviewed_geography_fixture()
    members: dict[str, dict[str, object]] = {}
    rows: dict[str, int] = {}
    for name, schema in ANNUAL_MEMBER_SCHEMAS.items():
        path = generation / f"{name}.parquet"
        pl.DataFrame(schema=dict(schema)).write_parquet(path)
        rows[name] = 0
        members[name] = {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    summary = {
        "calendar": {"complete_dates": 0, "catalogue_absent_dates": 0},
        "devices": 0,
        "threshold_grid_rows": 0,
        "output_rows": rows,
    }
    _write_json(generation / "summary.json", summary)
    summary_path = generation / "summary.json"
    identity = {
        "schema_version": 1,
        "analysis": "annual_micro_sensor_readiness",
        "config": {},
        "inputs": {"reviewed_geography_sha256": _geography_hash(geography)},
        "checkpoint_inventory": [],
        "claim_boundary": {
            "calibration_fitted": False,
            "bias_estimated": False,
            "fusion_performed": False,
            "satellite_acquired": False,
            "values_imputed": False,
            "nearest_reference_is_colocated_ground_truth": False,
            "high_resolution_pm25_created": False,
        },
        "output_rows": rows,
        "members": members,
        "summary_file": {
            "path": "summary.json",
            "bytes": summary_path.stat().st_size,
            "sha256": sha256_file(summary_path),
        },
    }
    generation_sha256 = _canonical_hash(identity)
    manifest = {
        **identity,
        "complete": True,
        "generated_at": "2026-08-13T00:00:00+00:00",
        "generation_sha256": generation_sha256,
        "git_sha": "0" * 40,
        "git_dirty": False,
        "checkpoint_run": [],
    }
    _write_json(generation / "manifest.json", manifest)
    final = tmp_path / generation_sha256
    generation.replace(final)
    generation = final
    return generation, generation_sha256, geography


def _fixture_config(
    agreement: Any,
    generation_sha256: str,
    *,
    primary_devices: int = 0,
    primary_stations: int = 0,
) -> Any:
    return replace(
        agreement.load_annual_agreement_config(),
        annual_generation_sha256=generation_sha256,
        primary_devices=primary_devices,
        primary_stations=primary_stations,
    )


def _rebind_generation(generation: Path) -> tuple[Path, str]:
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: dict[str, int] = {}
    for name in ANNUAL_MEMBER_SCHEMAS:
        path = generation / f"{name}.parquet"
        manifest["members"][name] = {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        rows[name] = pl.scan_parquet(path).select(pl.len()).collect().item()
    summary_path = generation / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["output_rows"] = rows
    summary["devices"] = rows["device_cohorts"]
    summary["threshold_grid_rows"] = rows["cohort_thresholds"]
    _write_json(summary_path, summary)
    manifest["output_rows"] = rows
    manifest["summary_file"] = {
        "path": "summary.json",
        "bytes": summary_path.stat().st_size,
        "sha256": sha256_file(summary_path),
    }
    identity = {
        field: manifest[field]
        for field in manifest
        if field
        in {
            "schema_version",
            "analysis",
            "config",
            "inputs",
            "checkpoint_inventory",
            "claim_boundary",
            "output_rows",
            "members",
            "summary_file",
        }
    }
    generation_sha256 = _canonical_hash(identity)
    manifest["generation_sha256"] = generation_sha256
    _write_json(manifest_path, manifest)
    rebound = generation.parent / generation_sha256
    generation.replace(rebound)
    return rebound, generation_sha256


def _cohort_fixture(*, devices: int, stations: int) -> pl.DataFrame:
    values: dict[str, list[object]] = {}
    for name, dtype in ANNUAL_DEVICE_COHORT_SCHEMA:
        if name == "device_id":
            values[name] = [f"device-{index:03d}" for index in range(devices)]
        elif name == "station_name":
            values[name] = [f"station-{index % stations:02d}" for index in range(devices)]
        elif name == "distance_km":
            values[name] = [0.5] * devices
        elif name == "spatial_state":
            values[name] = ["eligible"] * devices
        elif name == "active_months":
            values[name] = [3] * devices
        elif name == "trio_dates":
            values[name] = [30] * devices
        elif name == "trio_observed_hours":
            values[name] = [360] * devices
        elif dtype == pl.String:
            values[name] = [None] * devices
        elif dtype == pl.Float64:
            values[name] = [0.0] * devices
        else:
            values[name] = [0] * devices
    return pl.DataFrame(values, schema=dict(ANNUAL_DEVICE_COHORT_SCHEMA))


def _device_day_fixture(device_id: str) -> pl.DataFrame:
    values: dict[str, list[object]] = {}
    for name, dtype in ANNUAL_DEVICE_DAY_SCHEMA:
        if name == "date":
            values[name] = [date(2025, 1, 1)]
        elif name == "device_id":
            values[name] = [device_id]
        elif name == "spatial_state":
            values[name] = ["eligible"]
        elif name == "station_name":
            values[name] = ["station-a"]
        elif dtype == pl.String:
            values[name] = [None]
        elif dtype == pl.Float64:
            values[name] = [0.0]
        else:
            values[name] = [0]
    return pl.DataFrame(values, schema=dict(ANNUAL_DEVICE_DAY_SCHEMA))


def _agreement_micro_member(variable: str) -> pl.DataFrame:
    devices = ("eligible", "ground-absent", "ground-ineligible")
    rows: list[dict[str, object]] = []
    row_number = 1
    value = {"pm25": 20.0, "humidity": 60.0, "temperature": 25.0}[variable]
    for device_id in devices:
        for hour in range(18):
            for minute in range(60):
                rows.append(
                    {
                        "source_row_number": row_number,
                        "device_id": device_id,
                        "variable": variable,
                        "ts_local": datetime(2025, 1, 2, hour, minute),
                        "value": value,
                        "lon": 121.5,
                        "lat": 25.0,
                        "coordinate_wgs84_valid": True,
                    }
                )
                row_number += 1
    return pl.DataFrame(rows, schema=dict(OBSERVATION_OUTPUT_SCHEMA))


def _agreement_ground_member() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for station_name, flag in (("station-a", "valid"), ("station-c", "invalid")):
        for hour in range(18):
            rows.append(
                {
                    "station_name": station_name,
                    "pollutant": "PM2.5",
                    "ts_local": datetime(2025, 1, 2, hour),
                    "value": 18.0,
                    "flag": flag,
                    "value_retained": True,
                    "imputed": False,
                    "impute_method": None,
                    "generation": "fixture",
                    "source_member": "fixture.csv",
                }
            )
    return pl.DataFrame(rows).select(
        *(pl.col(name).cast(dtype).alias(name) for name, dtype in PARTITION_SCHEMA.items())
    )


def _agreement_annual_days() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    ground_counts = {
        "eligible": (18, 18, 0, 0),
        "ground-absent": (0, 0, 0, 18),
        "ground-ineligible": (18, 0, 18, 0),
    }
    for device_id, station_name, distance_km in AGREEMENT_CANDIDATES.iter_rows():
        if device_id == "device-absent":
            continue
        row: dict[str, object] = {"date": AGREEMENT_DAY, "device_id": device_id}
        for variable in ("pm25", "humidity", "temperature"):
            row.update(
                {
                    f"{variable}_source_rows": 1080,
                    f"{variable}_null_value_rows": 0,
                    f"{variable}_null_timestamp_rows": 0,
                    f"{variable}_distinct_timestamps": 1080,
                    f"{variable}_observed_hours": 18,
                    f"{variable}_duplicate_timestamp_groups": 0,
                    f"{variable}_extreme_rows": 0,
                }
            )
        present, eligible, present_ineligible, absent = ground_counts[str(device_id)]
        row.update(
            {
                "trio_observed_hours": 18,
                "coordinate_source_rows": 1080,
                "coordinate_null_rows": 0,
                "coordinate_invalid_rows": 0,
                "coordinate_positions": 1,
                "lon_min": 121.5,
                "lon_max": 121.5,
                "lat_min": 25.0,
                "lat_max": 25.0,
                "spatial_state": "eligible",
                "station_name": station_name,
                "distance_km": distance_km,
                "ground_present_trio_hours": present,
                "ground_eligible_trio_hours": eligible,
                "ground_present_ineligible_trio_hours": present_ineligible,
                "ground_absent_trio_hours": absent,
            }
        )
        rows.append(row)
    return pl.DataFrame(rows, schema=dict(ANNUAL_DEVICE_DAY_SCHEMA))


def _write_agreement_day_inputs(tmp_path: Path) -> tuple[dict[str, Path], Path, Path]:
    micro_paths: dict[str, Path] = {}
    for variable in ("pm25", "humidity", "temperature"):
        path = tmp_path / f"{variable}.parquet"
        _agreement_micro_member(variable).write_parquet(path)
        micro_paths[variable] = path
    annual_path = tmp_path / "annual-device-days.parquet"
    _agreement_annual_days().write_parquet(annual_path)
    ground_path = tmp_path / "ground.parquet"
    _agreement_ground_member().write_parquet(ground_path)
    return micro_paths, annual_path, ground_path


def _single_agreement_inputs(
    tmp_path: Path,
    *,
    micro_mutation: str | None = None,
    ground_mutation: str | None = None,
) -> tuple[dict[str, Path], Path, Path, pl.DataFrame]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    candidates = AGREEMENT_CANDIDATES.filter(pl.col("device_id") == "eligible")
    annual = _agreement_annual_days().filter(pl.col("device_id") == "eligible")
    micro_paths: dict[str, Path] = {}
    for variable in ("pm25", "humidity", "temperature"):
        frame = _agreement_micro_member(variable).filter(pl.col("device_id") == "eligible")
        if variable == "pm25" and micro_mutation == "null_value":
            frame = (
                frame.with_row_index()
                .with_columns(
                    pl.when(pl.col("index") == 0)
                    .then(pl.lit(None, dtype=pl.Float64))
                    .otherwise(pl.col("value"))
                    .alias("value")
                )
                .drop("index")
            )
            annual = annual.with_columns(pl.lit(1).alias("pm25_null_value_rows"))
        elif variable == "pm25" and micro_mutation == "null_timestamp":
            frame = (
                frame.with_row_index()
                .with_columns(
                    pl.when(pl.col("index") == 0)
                    .then(pl.lit(None, dtype=pl.Datetime("us")))
                    .otherwise(pl.col("ts_local"))
                    .alias("ts_local")
                )
                .drop("index")
            )
            annual = annual.with_columns(
                pl.lit(1).alias("pm25_null_timestamp_rows"),
                pl.lit(1079).alias("pm25_distinct_timestamps"),
            )
        elif variable == "pm25" and micro_mutation == "duplicate_timestamp":
            duplicate = frame["ts_local"][0]
            frame = (
                frame.with_row_index()
                .with_columns(
                    pl.when(pl.col("index") == 1)
                    .then(pl.lit(duplicate))
                    .otherwise(pl.col("ts_local"))
                    .alias("ts_local")
                )
                .drop("index")
            )
            annual = annual.with_columns(
                pl.lit(1079).alias("pm25_distinct_timestamps"),
                pl.lit(1).alias("pm25_duplicate_timestamp_groups"),
            )
        elif variable == "pm25" and micro_mutation == "extreme_value":
            frame = (
                frame.with_row_index()
                .with_columns(
                    pl.when(pl.col("index") == 0)
                    .then(pl.lit(1001.0))
                    .otherwise(pl.col("value"))
                    .alias("value")
                )
                .drop("index")
            )
            annual = annual.with_columns(pl.lit(1).alias("pm25_extreme_rows"))
        elif variable == "pm25" and micro_mutation == "short_source":
            frame = frame.slice(0, 1079)
            annual = annual.with_columns(
                pl.lit(1079).alias("pm25_source_rows"),
                pl.lit(1079).alias("pm25_distinct_timestamps"),
                pl.lit(1079).alias("coordinate_source_rows"),
            )
        elif variable == "pm25" and micro_mutation == "short_hours":
            frame = (
                frame.with_row_index()
                .with_columns(
                    (pl.lit(datetime(2025, 1, 2)) + pl.duration(seconds=pl.col("index")))
                    .cast(pl.Datetime("us"))
                    .alias("ts_local")
                )
                .drop("index")
            )
            annual = annual.with_columns(
                pl.lit(1).alias("pm25_observed_hours"),
                pl.lit(1).alias("trio_observed_hours"),
                pl.lit(1).alias("ground_present_trio_hours"),
                pl.lit(1).alias("ground_eligible_trio_hours"),
            )
        path = tmp_path / f"{variable}.parquet"
        frame.write_parquet(path)
        micro_paths[variable] = path
    ground = _agreement_ground_member().filter(pl.col("station_name") == "station-a")
    if ground_mutation == "invalid_flag":
        ground = ground.with_columns(pl.lit("invalid").cast(pl.Categorical).alias("flag"))
        annual = annual.with_columns(
            pl.lit(0).alias("ground_eligible_trio_hours"),
            pl.lit(18).alias("ground_present_ineligible_trio_hours"),
        )
    elif ground_mutation == "null_value":
        ground = ground.with_columns(pl.lit(None, dtype=pl.Float32).alias("value"))
        annual = annual.with_columns(
            pl.lit(0).alias("ground_eligible_trio_hours"),
            pl.lit(18).alias("ground_present_ineligible_trio_hours"),
        )
    elif ground_mutation == "non_finite":
        ground = ground.with_columns(pl.lit(float("inf"), dtype=pl.Float32).alias("value"))
        annual = annual.with_columns(
            pl.lit(0).alias("ground_eligible_trio_hours"),
            pl.lit(18).alias("ground_present_ineligible_trio_hours"),
        )
    elif ground_mutation == "absent":
        ground = ground.clear()
        annual = annual.with_columns(
            pl.lit(0).alias("ground_present_trio_hours"),
            pl.lit(0).alias("ground_eligible_trio_hours"),
            pl.lit(18).alias("ground_absent_trio_hours"),
        )
    annual_path = tmp_path / "annual-device-days.parquet"
    annual.cast(dict(ANNUAL_DEVICE_DAY_SCHEMA), strict=True).write_parquet(annual_path)
    ground_path = tmp_path / "ground.parquet"
    ground.write_parquet(ground_path)
    return micro_paths, annual_path, ground_path, candidates


def _aggregate_single_agreement_day(
    tmp_path: Path,
    *,
    micro_mutation: str | None = None,
    ground_mutation: str | None = None,
) -> Any:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path, candidates = _single_agreement_inputs(
        tmp_path,
        micro_mutation=micro_mutation,
        ground_mutation=ground_mutation,
    )
    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)
    return agreement._aggregate_agreement_day(
        day=AGREEMENT_DAY,
        micro_paths=micro_paths,
        annual_device_days=_pinned_annual_member(agreement, annual_path),
        ground_path=ground_path,
        candidates=candidates,
        input_identities=identities,
        config=replace(
            agreement.load_annual_agreement_config(),
            primary_devices=1,
            primary_stations=1,
        ),
    )


def _checkpoint_input_identities(
    micro_paths: dict[str, Path],
    annual_path: Path,
    ground_path: Path,
) -> dict[str, object]:
    source_members = {
        variable: {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for variable, path in sorted(micro_paths.items())
    }
    return {
        "raw_generation_sha256": "a" * 64,
        "parsed_generation_sha256": "b" * 64,
        "source_members": source_members,
        "ground_member": {
            "path": ground_path.name,
            "bytes": ground_path.stat().st_size,
            "sha256": sha256_file(ground_path),
        },
        "annual_generation_sha256": ANNUAL_GENERATION,
        "annual_device_days": {
            "path": annual_path.name,
            "bytes": annual_path.stat().st_size,
            "sha256": sha256_file(annual_path),
        },
        "candidate_identity_sha256": _canonical_hash(
            AGREEMENT_CANDIDATES.filter(pl.col("device_id") == "eligible").to_dicts()
        ),
        "catalogue_state": "present",
        "annual_selected_date_rows": pl.scan_parquet(annual_path)
        .filter(pl.col("date") == AGREEMENT_DAY)
        .select(pl.len())
        .collect()
        .item(),
    }


def _checkpoint_fixture(
    tmp_path: Path,
) -> tuple[Any, dict[str, object], Any]:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path, _ = _single_agreement_inputs(tmp_path / "inputs")
    result = _aggregate_single_agreement_day(tmp_path / "aggregation")
    config = replace(
        agreement.load_annual_agreement_config(),
        primary_devices=1,
        primary_stations=1,
    )
    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)
    return result, identities, config


def _pinned_annual_member(agreement: Any, annual_path: Path) -> Any:
    generation_dir = annual_path.parent.resolve()
    return agreement.PinnedAnnualMember(
        path=annual_path.resolve(),
        generation_dir=generation_dir,
        bytes=annual_path.stat().st_size,
        sha256=sha256_file(annual_path),
    )


def test_a_day_retains_every_candidate_and_separates_absent_withheld_and_eligible_values(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path = _write_agreement_day_inputs(tmp_path)
    config = replace(
        agreement.load_annual_agreement_config(),
        primary_devices=AGREEMENT_CANDIDATES.height,
        primary_stations=AGREEMENT_CANDIDATES["station_name"].n_unique(),
    )

    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)
    result = agreement._aggregate_agreement_day(
        day=AGREEMENT_DAY,
        micro_paths=micro_paths,
        annual_device_days=_pinned_annual_member(agreement, annual_path),
        ground_path=ground_path,
        candidates=AGREEMENT_CANDIDATES,
        input_identities={
            **identities,
            "candidate_identity_sha256": _canonical_hash(
                AGREEMENT_CANDIDATES.sort("device_id").to_dicts()
            ),
        },
        config=config,
    )

    assert result.rows.height == AGREEMENT_CANDIDATES.height
    assert result.rows.filter(pl.col("reason") == "device_day_absent").height == 1
    assert result.rows.filter(pl.col("reason") == "ground_absent").height == 1
    assert result.rows.filter(pl.col("reason") == "ground_present_but_ineligible").height == 1
    eligible = result.rows.filter(pl.col("reason") == "eligible")
    assert eligible["micro_pm25_mean"].null_count() == 0
    assert eligible["micro_humidity_mean"].null_count() == 0
    assert eligible["micro_temperature_mean"].null_count() == 0
    assert eligible["ground_pm25_mean"].null_count() == 0


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("null_value", "pm25_null_value"),
        ("null_timestamp", "pm25_null_timestamp"),
        ("duplicate_timestamp", "pm25_duplicate_timestamp"),
        ("extreme_value", "pm25_extreme_value"),
        ("short_source", "pm25_insufficient_source_rows"),
        ("short_hours", "pm25_insufficient_observed_hours"),
    ],
)
def test_each_micro_source_failure_withholds_the_mean_without_replacing_it_with_zero(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    result = _aggregate_single_agreement_day(tmp_path, micro_mutation=mutation)

    assert result.rows["reason"].to_list() == [reason]
    assert result.rows["micro_pm25_mean"].to_list() == [None]
    assert result.rows["ground_pm25_mean"].to_list() == [None]


@pytest.mark.parametrize("mutation", ["invalid_flag", "null_value"])
def test_a_present_but_ineligible_ground_day_is_not_zero_or_absent(
    tmp_path: Path,
    mutation: str,
) -> None:
    result = _aggregate_single_agreement_day(tmp_path, ground_mutation=mutation)

    assert result.rows["reason"].to_list() == ["ground_present_but_ineligible"]
    assert result.rows["ground_pm25_mean"].to_list() == [None]
    assert result.rows["ground_present_trio_hours"].to_list() == [18]


def test_an_absent_ground_day_stays_distinct_from_present_but_ineligible(
    tmp_path: Path,
) -> None:
    result = _aggregate_single_agreement_day(tmp_path, ground_mutation="absent")

    assert result.rows["reason"].to_list() == ["ground_absent"]
    assert result.rows["ground_pm25_mean"].to_list() == [None]
    assert result.rows["ground_present_trio_hours"].to_list() == [0]


def test_a_non_finite_ground_value_is_retained_as_present_but_ineligible(
    tmp_path: Path,
) -> None:
    result = _aggregate_single_agreement_day(tmp_path, ground_mutation="non_finite")

    assert result.rows["reason"].to_list() == ["ground_present_but_ineligible"]
    assert result.rows["ground_eligible_trio_hours"].to_list() == [0]
    assert result.rows["ground_present_ineligible_trio_hours"].to_list() == [18]
    assert result.rows["ground_pm25_mean"].to_list() == [None]


def _catalogue_absent_identities(
    annual_path: Path,
    candidates: pl.DataFrame,
) -> dict[str, object]:
    return {
        "raw_generation_sha256": "a" * 64,
        "parsed_generation_sha256": "b" * 64,
        "source_members": None,
        "ground_member": None,
        "annual_generation_sha256": ANNUAL_GENERATION,
        "annual_device_days": {
            "path": annual_path.name,
            "bytes": annual_path.stat().st_size,
            "sha256": sha256_file(annual_path),
        },
        "candidate_identity_sha256": _canonical_hash(candidates.sort("device_id").to_dicts()),
        "catalogue_state": "absent",
        "annual_selected_date_rows": 0,
    }


def test_a_catalogue_absent_date_proves_the_pinned_annual_ledger_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    absent = date(2025, 1, 26)
    annual_path = tmp_path / "device_days.parquet"
    pl.DataFrame(schema=dict(ANNUAL_DEVICE_DAY_SCHEMA)).write_parquet(annual_path)
    pinned = _pinned_annual_member(agreement, annual_path)
    config = replace(
        agreement.load_annual_agreement_config(),
        primary_devices=AGREEMENT_CANDIDATES.height,
        primary_stations=AGREEMENT_CANDIDATES["station_name"].n_unique(),
    )
    entered = 0
    stable = agreement.stable_annual_member_path

    @contextmanager
    def record_stable_member(member: Any) -> Any:
        nonlocal entered
        entered += 1
        with stable(member) as path:
            yield path

    monkeypatch.setattr(agreement, "stable_annual_member_path", record_stable_member)

    result = agreement._aggregate_agreement_day(
        day=absent,
        micro_paths=None,
        annual_device_days=pinned,
        ground_path=None,
        candidates=AGREEMENT_CANDIDATES,
        input_identities=_catalogue_absent_identities(annual_path, AGREEMENT_CANDIDATES),
        config=config,
    )

    assert entered == 1
    assert result.rows.height == AGREEMENT_CANDIDATES.height
    assert result.rows["reason"].to_list() == ["catalogue_absent"] * 4
    assert result.rows["micro_pm25_mean"].null_count() == 4
    assert result.input_identities["source_members"] is None
    assert result.input_identities["ground_member"] is None
    assert result.input_identities["annual_selected_date_rows"] == 0


def test_a_catalogue_absent_date_rejects_a_contradictory_annual_ledger(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    absent = date(2025, 1, 26)
    annual_path = tmp_path / "device_days.parquet"
    _device_day_fixture("eligible").with_columns(pl.lit(absent).alias("date")).write_parquet(
        annual_path
    )

    with pytest.raises(RuntimeError, match="catalogue absence contradicts annual device-days"):
        agreement._aggregate_agreement_day(
            day=absent,
            micro_paths=None,
            annual_device_days=_pinned_annual_member(agreement, annual_path),
            ground_path=None,
            candidates=AGREEMENT_CANDIDATES,
            input_identities=_catalogue_absent_identities(annual_path, AGREEMENT_CANDIDATES),
            config=replace(
                agreement.load_annual_agreement_config(),
                primary_devices=AGREEMENT_CANDIDATES.height,
                primary_stations=AGREEMENT_CANDIDATES["station_name"].n_unique(),
            ),
        )


def test_a_catalogue_absent_date_rejects_a_nonexistent_pinned_annual_member(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    missing = tmp_path / "missing-device-days.parquet"
    member = agreement.PinnedAnnualMember(
        path=missing.resolve(),
        generation_dir=tmp_path.resolve(),
        bytes=0,
        sha256="0" * 64,
    )

    with pytest.raises(RuntimeError, match="annual readiness member is unreadable"):
        agreement._aggregate_agreement_day(
            day=date(2025, 1, 26),
            micro_paths=None,
            annual_device_days=member,
            ground_path=None,
            candidates=AGREEMENT_CANDIDATES,
            input_identities={
                "raw_generation_sha256": "a" * 64,
                "parsed_generation_sha256": "b" * 64,
                "source_members": None,
                "ground_member": None,
                "annual_generation_sha256": ANNUAL_GENERATION,
                "annual_device_days": {
                    "path": missing.name,
                    "bytes": 0,
                    "sha256": "0" * 64,
                },
                "candidate_identity_sha256": _canonical_hash(
                    AGREEMENT_CANDIDATES.sort("device_id").to_dicts()
                ),
                "catalogue_state": "absent",
                "annual_selected_date_rows": 0,
            },
            config=replace(
                agreement.load_annual_agreement_config(),
                primary_devices=AGREEMENT_CANDIDATES.height,
                primary_stations=AGREEMENT_CANDIDATES["station_name"].n_unique(),
            ),
        )


def test_the_writer_rejects_present_identities_on_a_catalogue_absent_result(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    absent = date(2025, 1, 26)
    annual_path = tmp_path / "device_days.parquet"
    pl.DataFrame(schema=dict(ANNUAL_DEVICE_DAY_SCHEMA)).write_parquet(annual_path)
    config = replace(
        agreement.load_annual_agreement_config(),
        primary_devices=AGREEMENT_CANDIDATES.height,
        primary_stations=AGREEMENT_CANDIDATES["station_name"].n_unique(),
    )
    result = agreement._aggregate_agreement_day(
        day=absent,
        micro_paths=None,
        annual_device_days=_pinned_annual_member(agreement, annual_path),
        ground_path=None,
        candidates=AGREEMENT_CANDIDATES,
        input_identities=_catalogue_absent_identities(annual_path, AGREEMENT_CANDIDATES),
        config=config,
    )
    fabricated_file = {"path": "fabricated.parquet", "bytes": 0, "sha256": "0" * 64}
    changed = {
        **result.input_identities,
        "catalogue_state": "present",
        "source_members": dict.fromkeys(("pm25", "humidity", "temperature"), fabricated_file),
        "ground_member": fabricated_file,
    }

    with pytest.raises(RuntimeError, match="catalogue state and input paths disagree"):
        agreement._write_agreement_day_checkpoint(
            replace(result, input_identities=changed),
            day=absent,
            checkpoint_root=tmp_path / "checkpoints",
        )
    assert not (tmp_path / "checkpoints").exists()


def test_a_recomputed_source_count_that_differs_from_annual_device_days_fails_closed(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path, candidates = _single_agreement_inputs(tmp_path)
    annual = pl.read_parquet(annual_path).with_columns(pl.lit(1079).alias("pm25_source_rows"))
    annual.cast(dict(ANNUAL_DEVICE_DAY_SCHEMA), strict=True).write_parquet(annual_path)

    with pytest.raises(RuntimeError, match="derived device-day counts changed"):
        agreement._aggregate_agreement_day(
            day=AGREEMENT_DAY,
            micro_paths=micro_paths,
            annual_device_days=_pinned_annual_member(agreement, annual_path),
            ground_path=ground_path,
            candidates=candidates,
            input_identities=_checkpoint_input_identities(micro_paths, annual_path, ground_path),
            config=replace(
                agreement.load_annual_agreement_config(),
                primary_devices=1,
                primary_stations=1,
            ),
        )


def test_the_public_day_aggregator_requires_the_reviewed_protocol_and_pinned_annual_member(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path, candidates = _single_agreement_inputs(tmp_path)
    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)
    altered = replace(
        agreement.load_annual_agreement_config(),
        primary_devices=1,
        primary_stations=1,
    )

    with pytest.raises(RuntimeError, match="reviewed protocol changed"):
        agreement.aggregate_agreement_day(
            day=AGREEMENT_DAY,
            micro_paths=micro_paths,
            annual_device_days=_pinned_annual_member(agreement, annual_path),
            ground_path=ground_path,
            candidates=candidates,
            input_identities=identities,
            config=altered,
        )
    with pytest.raises(TypeError):
        agreement.aggregate_agreement_day(
            day=AGREEMENT_DAY,
            micro_paths=micro_paths,
            annual_device_days=annual_path,
            ground_path=ground_path,
            candidates=candidates,
            input_identities=identities,
            config=agreement.load_annual_agreement_config(),
        )
    with pytest.raises(RuntimeError, match="reviewed candidate cohort changed"):
        agreement.aggregate_agreement_day(
            day=AGREEMENT_DAY,
            micro_paths=micro_paths,
            annual_device_days=_pinned_annual_member(agreement, annual_path),
            ground_path=ground_path,
            candidates=candidates,
            input_identities=identities,
            config=agreement.load_annual_agreement_config(),
        )


def test_the_public_day_aggregator_rejects_a_fabricated_124_device_13_station_cohort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path, _ = _single_agreement_inputs(tmp_path)
    trusted = _cohort_fixture(devices=124, stations=13).select(
        "device_id", "station_name", "distance_km"
    )
    fabricated = trusted.with_columns(
        (pl.col("device_id") + pl.lit("-fabricated")).alias("device_id")
    )
    pinned = _pinned_annual_member(agreement, annual_path)
    readiness = agreement.AnnualReadinessInput(
        generation_dir=pinned.generation_dir,
        manifest={},
        summary={},
        calendar_coverage=pl.DataFrame(),
        device_days=pinned,
        device_cohorts=pl.DataFrame(),
        cohort_thresholds=pl.DataFrame(),
        exclusions=pl.DataFrame(),
        candidate_cohorts=(agreement.AnnualDistanceCohort(0.5, trusted),),
    )
    monkeypatch.setattr(
        agreement,
        "load_annual_readiness_input",
        lambda generation_dir: readiness,
    )
    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)
    identities["candidate_identity_sha256"] = _canonical_hash(
        fabricated.sort("device_id").to_dicts()
    )

    with pytest.raises(RuntimeError, match="exact reviewed primary cohort changed"):
        agreement.aggregate_agreement_day(
            day=AGREEMENT_DAY,
            micro_paths=micro_paths,
            annual_device_days=pinned,
            ground_path=ground_path,
            candidates=fabricated,
            input_identities=identities,
            config=agreement.load_annual_agreement_config(),
        )


def test_aggregation_rejects_an_identity_that_does_not_match_the_member_it_reads(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path, candidates = _single_agreement_inputs(tmp_path)
    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)
    source_members = deepcopy(identities["source_members"])
    assert isinstance(source_members, dict)
    assert isinstance(source_members["pm25"], dict)
    source_members["pm25"]["sha256"] = "d" * 64
    identities["source_members"] = source_members

    with pytest.raises(RuntimeError, match="pm25 source member changed"):
        agreement._aggregate_agreement_day(
            day=AGREEMENT_DAY,
            micro_paths=micro_paths,
            annual_device_days=_pinned_annual_member(agreement, annual_path),
            ground_path=ground_path,
            candidates=candidates,
            input_identities=identities,
            config=replace(
                agreement.load_annual_agreement_config(),
                primary_devices=1,
                primary_stations=1,
            ),
        )


def test_a_spill_cleanup_failure_is_reported_instead_of_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path, candidates = _single_agreement_inputs(tmp_path)
    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)
    real_rmtree = agreement.shutil.rmtree
    failed = False

    def fail_spill_once(path: str | Path, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if Path(path).name == "duckdb-spill" and not failed:
            failed = True
            raise OSError("measured spill cleanup failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(agreement.shutil, "rmtree", fail_spill_once)
    with pytest.raises(OSError, match="spill cleanup failure"):
        agreement._aggregate_agreement_day(
            day=AGREEMENT_DAY,
            micro_paths=micro_paths,
            annual_device_days=_pinned_annual_member(agreement, annual_path),
            ground_path=ground_path,
            candidates=candidates,
            input_identities=identities,
            config=replace(
                agreement.load_annual_agreement_config(),
                primary_devices=1,
                primary_stations=1,
            ),
        )


def test_a_checkpoint_first_write_binds_every_identity_and_exact_reuse_changes_nothing(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    result, identities, config = _checkpoint_fixture(tmp_path)
    checkpoint_root = tmp_path / "checkpoints"

    written = agreement._write_agreement_day_checkpoint(
        result,
        day=AGREEMENT_DAY,
        checkpoint_root=checkpoint_root,
    )
    member_stat = written.member_path.stat()
    manifest_stat = written.manifest_path.stat()
    member_hash = sha256_file(written.member_path)
    manifest_hash = sha256_file(written.manifest_path)
    time.sleep(0.02)
    reused = agreement._write_agreement_day_checkpoint(
        result,
        day=AGREEMENT_DAY,
        checkpoint_root=checkpoint_root,
    )

    assert reused.directory == written.directory
    assert reused.member_path == written.member_path
    assert reused.manifest_path == written.manifest_path
    assert reused.manifest == written.manifest
    assert reused.rows.equals(result.rows)
    assert reused.manifest["date"] == AGREEMENT_DAY.isoformat()
    assert reused.manifest["inputs"] == identities
    assert reused.manifest["config"] == json.loads(json.dumps(asdict(config)))
    assert reused.manifest["rows"] == result.rows.height
    assert reused.manifest["schema"] == {
        name: str(dtype) for name, dtype in agreement.AGREEMENT_DAY_SCHEMA
    }
    assert reused.manifest["summary"] == result.summary
    assert reused.manifest["summary_sha256"] == _canonical_hash(result.summary)
    assert reused.manifest["member"] == {
        "path": "paired_day.parquet",
        "bytes": reused.member_path.stat().st_size,
        "sha256": member_hash,
    }
    assert reused.member_path.stat().st_mtime_ns == member_stat.st_mtime_ns
    assert reused.manifest_path.stat().st_mtime_ns == manifest_stat.st_mtime_ns
    assert sha256_file(reused.member_path) == member_hash
    assert sha256_file(reused.manifest_path) == manifest_hash


def test_a_checkpoint_cannot_be_reused_after_any_bound_input_identity_changes(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    result, identities, config = _checkpoint_fixture(tmp_path)
    checkpoint_root = tmp_path / "checkpoints"
    agreement._write_agreement_day_checkpoint(
        result,
        day=AGREEMENT_DAY,
        checkpoint_root=checkpoint_root,
    )
    changed = deepcopy(identities)
    changed["raw_generation_sha256"] = "c" * 64

    with pytest.raises(RuntimeError, match="checkpoint input identity changed"):
        agreement._load_agreement_day_checkpoint(
            day=AGREEMENT_DAY,
            input_identities=changed,
            config=config,
            checkpoint_root=checkpoint_root,
        )
    with pytest.raises(RuntimeError, match="reviewed protocol changed"):
        agreement.load_agreement_day_checkpoint(
            day=AGREEMENT_DAY,
            input_identities=identities,
            config=config,
            checkpoint_root=checkpoint_root,
        )


def test_the_writer_revalidates_the_physical_inputs_recorded_by_aggregation(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    result, _, _ = _checkpoint_fixture(tmp_path)
    changed = deepcopy(result.input_identities)
    changed["source_members"]["pm25"]["sha256"] = "e" * 64

    with pytest.raises(RuntimeError, match="pm25 source member changed"):
        agreement._write_agreement_day_checkpoint(
            replace(result, input_identities=changed),
            day=AGREEMENT_DAY,
            checkpoint_root=tmp_path / "checkpoints",
        )


def test_a_checkpoint_rejects_member_mutation_and_leaves_no_staging_or_spill_residue(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    result, identities, config = _checkpoint_fixture(tmp_path)
    checkpoint_root = tmp_path / "checkpoints"
    written = agreement._write_agreement_day_checkpoint(
        result,
        day=AGREEMENT_DAY,
        checkpoint_root=checkpoint_root,
    )
    written.member_path.write_bytes(written.member_path.read_bytes() + b"changed")

    with pytest.raises(RuntimeError, match="checkpoint member changed"):
        agreement._load_agreement_day_checkpoint(
            day=AGREEMENT_DAY,
            input_identities=identities,
            config=config,
            checkpoint_root=checkpoint_root,
        )
    assert not list(checkpoint_root.glob(".*.staging-*"))
    assert not list(checkpoint_root.glob(".*.backup-*"))
    assert not list(checkpoint_root.rglob("*duckdb*"))


@pytest.mark.parametrize("field", ["schema_version", "rows", "summary"])
def test_checkpoint_json_numbers_require_exact_integer_types(
    tmp_path: Path,
    field: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    result, identities, config = _checkpoint_fixture(tmp_path)
    checkpoint_root = tmp_path / "checkpoints"
    written = agreement._write_agreement_day_checkpoint(
        result,
        day=AGREEMENT_DAY,
        checkpoint_root=checkpoint_root,
    )
    manifest = json.loads(written.manifest_path.read_text(encoding="utf-8"))
    if field == "schema_version":
        manifest[field] = 1.0
    elif field == "rows":
        manifest[field] = True
    else:
        manifest[field]["eligible"] = True
        manifest["summary_sha256"] = _canonical_hash(manifest[field])
    _write_json(written.manifest_path, manifest)

    with pytest.raises(RuntimeError, match=r"manifest|row count|summary"):
        agreement._load_agreement_day_checkpoint(
            day=AGREEMENT_DAY,
            input_identities=identities,
            config=config,
            checkpoint_root=checkpoint_root,
        )


def test_the_checkpoint_lock_rejects_same_process_and_subprocess_contention(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    lock_path = tmp_path / ".day.lock"
    code = (
        "from pathlib import Path; "
        "from twair.analysis.micro_sensor_annual_agreement import _agreement_checkpoint_lock; "
        f"p=Path({str(lock_path)!r}); "
        "\nwith _agreement_checkpoint_lock(p):\n print('acquired')"
    )

    with agreement._agreement_checkpoint_lock(lock_path):
        with (
            pytest.raises(RuntimeError, match="checkpoint writer is active"),
            agreement._agreement_checkpoint_lock(lock_path),
        ):
            pass
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
        )

    assert completed.returncode != 0
    assert "checkpoint writer is active" in completed.stderr


def test_a_protected_body_exception_releases_the_checkpoint_lock(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    lock_path = tmp_path / ".day.lock"

    with pytest.raises(KeyboardInterrupt), agreement._agreement_checkpoint_lock(lock_path):
        raise KeyboardInterrupt
    with agreement._agreement_checkpoint_lock(lock_path):
        pass


@pytest.mark.parametrize("interrupt_call", [1, 2])
def test_either_checkpoint_swap_rename_interrupt_restores_the_incomplete_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_call: int,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    result, _, _ = _checkpoint_fixture(tmp_path)
    checkpoint_root = tmp_path / "checkpoints"
    destination = checkpoint_root / AGREEMENT_DAY.isoformat()
    destination.mkdir(parents=True)
    baseline = destination / "incomplete.txt"
    baseline.write_text("recoverable", encoding="utf-8")
    real_replace = Path.replace
    calls = 0

    def interrupt_rename(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == interrupt_call:
            real_replace(path, target)
            raise KeyboardInterrupt
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupt_rename)
    with pytest.raises(KeyboardInterrupt):
        agreement._write_agreement_day_checkpoint(
            result,
            day=AGREEMENT_DAY,
            checkpoint_root=checkpoint_root,
        )

    assert baseline.read_text(encoding="utf-8") == "recoverable"
    assert not list(checkpoint_root.glob(".*.staging-*"))
    assert not list(checkpoint_root.glob(".*.backup-*"))


def test_the_reviewed_agreement_config_enforces_the_scientific_and_resource_boundary() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")

    config = agreement.load_annual_agreement_config()

    assert config.annual_generation_sha256 == ANNUAL_GENERATION
    assert config.distance_bands_km == (0.5, 1.0, 2.0)
    assert config.primary_distance_km == 0.5
    assert config.primary_devices == 124
    assert config.primary_stations == 13
    assert config.minimum_active_months == 3
    assert config.minimum_trio_dates == 30
    assert config.minimum_trio_hours == 360
    assert config.minimum_source_rows == 1080
    assert config.minimum_observed_hours == 18
    assert config.station_folds == 5
    assert config.quarters == (1, 2, 3, 4)
    assert config.ridge_alpha == 1.0
    assert config.threads == 1
    assert config.memory_limit_gb == 6
    assert dict(config.claim_boundary) == {
        "reference_station_agreement_only": True,
        "validated_calibration": False,
        "sensor_bias_estimate": False,
        "sensor_fusion": False,
        "colocated_ground_truth": False,
        "high_resolution_field": False,
        "satellite_feature_used": False,
        "values_imputed": False,
        "causal_analysis": False,
    }


def test_another_well_formed_annual_generation_is_rejected() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    raw = deepcopy(load_conf("micro_sensor_annual_agreement"))
    raw["analysis"]["annual_generation_sha256"] = "a" * 64

    with pytest.raises(ConfigError, match="annual generation"):
        agreement.load_annual_agreement_config(raw)


def test_the_annual_input_binds_all_five_members_and_rejects_changed_bytes(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)

    loaded = agreement._load_annual_readiness_input(
        generation,
        expected_generation_sha256=generation_sha256,
        reviewed_geography=geography,
        config=_fixture_config(agreement, generation_sha256),
    )

    assert loaded.manifest["generation_sha256"] == generation_sha256
    assert loaded.device_cohorts.height == 0
    assert loaded.device_days.sha256 == sha256_file(generation / "device_days.parquet")
    assert tuple(cohort.radius_km for cohort in loaded.candidate_cohorts) == (0.5, 1.0, 2.0)
    changed = generation / "device_days.parquet"
    changed.write_bytes(changed.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match="annual readiness member changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_an_extra_file_cannot_enter_the_annual_readiness_generation(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    (generation / "surprise.parquet").write_bytes(b"not reviewed")

    with pytest.raises(RuntimeError, match="file set changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_an_extra_directory_cannot_enter_the_annual_readiness_generation(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    (generation / ".staging-residue").mkdir()

    with pytest.raises(RuntimeError, match="file set changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_a_relabelled_annual_generation_directory_is_rejected(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    relabelled = tmp_path / ("b" * 64)
    generation.replace(relabelled)

    with pytest.raises(RuntimeError, match="directory identity changed"):
        agreement._load_annual_readiness_input(
            relabelled,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_an_unbound_manifest_field_is_rejected(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["surprise"] = "not part of the generation identity"
    _write_json(manifest_path, manifest)

    with pytest.raises(RuntimeError, match="manifest fields changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_candidate_devices_are_derived_from_reviewed_annual_thresholds_not_ids() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    config = replace(
        agreement.load_annual_agreement_config(),
        primary_devices=2,
        primary_stations=2,
    )
    cohorts = pl.DataFrame(
        {
            "device_id": ["near-a", "near-b", "mid", "sparse", "moving"],
            "station_name": ["station-a", "station-b", "station-b", "station-a", "station-a"],
            "distance_km": [0.1, 0.5, 1.5, 0.2, 0.2],
            "spatial_state": ["eligible", "eligible", "eligible", "eligible", "moving_coordinate"],
            "active_months": [3, 4, 3, 2, 3],
            "trio_dates": [30, 40, 30, 40, 40],
            "trio_observed_hours": [360, 480, 360, 480, 480],
        }
    )

    candidates = agreement.derive_agreement_candidates(cohorts, config=config)

    assert candidates.select("device_id").to_series().to_list() == ["mid", "near-a", "near-b"]
    assert candidates.filter(pl.col("distance_km") <= 0.5).height == 2
    assert candidates.filter(pl.col("distance_km") <= 1.0).height == 2
    assert candidates.filter(pl.col("distance_km") <= 2.0).height == 3


def test_a_manifest_change_during_member_reads_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    original_read = pl.read_parquet
    changed = False

    def read_and_change_manifest(path: str | Path) -> pl.DataFrame:
        nonlocal changed
        result = original_read(path)
        if not changed:
            changed = True
            manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(pl, "read_parquet", read_and_change_manifest)
    with pytest.raises(RuntimeError, match="manifest changed during read"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_declared_output_rows_must_equal_the_parquet_members(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["output_rows"]["device_cohorts"] = 1
    identity = {field: manifest[field] for field in agreement._ANNUAL_IDENTITY_FIELDS}
    generation_sha256 = _canonical_hash(identity)
    manifest["generation_sha256"] = generation_sha256
    _write_json(manifest_path, manifest)
    renamed = generation.parent / generation_sha256
    generation.replace(renamed)
    generation = renamed

    with pytest.raises(RuntimeError, match="row counts changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_a_geography_provenance_change_cannot_reuse_the_annual_generation(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    changed = geography.with_columns(pl.lit("reviewed_historical").alias("geo_source"))

    with pytest.raises(RuntimeError, match="geography identity changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=changed,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_multiple_reviewed_geography_rows_form_one_stable_identity() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    geography = pl.concat(
        [
            _reviewed_geography_fixture(),
            pl.DataFrame(
                {
                    "station_name": ["station-b"],
                    "lon": [120.5],
                    "lat": [23.5],
                    "geo_source": ["reviewed_historical"],
                    "geo_source_record_namespace": ["AIRTW central station detail"],
                    "geo_source_record_id": ["2"],
                }
            ),
        ]
    )

    assert agreement._geography_identity(geography) == _geography_hash(geography)


def test_a_parquet_member_change_during_its_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    original_read = pl.read_parquet
    changed = False

    def read_and_change_member(path: str | Path) -> pl.DataFrame:
        nonlocal changed
        result = original_read(path)
        path = Path(path)
        if not changed:
            changed = True
            path.write_bytes(path.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(pl, "read_parquet", read_and_change_member)
    with pytest.raises(RuntimeError, match="member changed during read"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_the_path_backed_device_day_member_stays_pinned_through_the_row_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    _device_day_fixture("reviewed-device").write_parquet(generation / "device_days.parquet")
    generation, generation_sha256 = _rebind_generation(generation)
    alternate = tmp_path / "alternate-device-days.parquet"
    _device_day_fixture("changed-device").write_parquet(alternate)
    original_scan = pl.scan_parquet
    changed = False

    def scan_after_replacement(path: str | Path) -> pl.LazyFrame:
        nonlocal changed
        if not changed and Path(path).name == "device_days.parquet":
            changed = True
            alternate.replace(path)
        return original_scan(path)

    monkeypatch.setattr(pl, "scan_parquet", scan_after_replacement)
    with pytest.raises(RuntimeError, match=r"device_days\.parquet changed during read"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


@pytest.mark.parametrize(
    ("devices", "stations", "message"),
    [
        (123, 13, "primary device count changed"),
        (124, 12, "primary station count changed"),
    ],
)
def test_loading_the_annual_input_enforces_the_reviewed_primary_cohort(
    tmp_path: Path,
    devices: int,
    stations: int,
    message: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    _cohort_fixture(devices=devices, stations=stations).write_parquet(
        generation / "device_cohorts.parquet"
    )
    generation, generation_sha256 = _rebind_generation(generation)

    with pytest.raises(RuntimeError, match=message):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(
                agreement,
                generation_sha256,
                primary_devices=124,
                primary_stations=13,
            ),
        )


def test_the_public_loader_does_not_accept_a_caller_supplied_generation_or_cohort_contract(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    _cohort_fixture(devices=123, stations=13).write_parquet(generation / "device_cohorts.parquet")
    generation, generation_sha256 = _rebind_generation(generation)
    public_loader: Any = agreement.load_annual_readiness_input

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        public_loader(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(
                agreement,
                generation_sha256,
                primary_devices=123,
                primary_stations=13,
            ),
        )


def test_a_new_generation_entry_created_during_loading_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    original_read = pl.read_parquet
    changed = False

    def read_and_add_entry(path: str | Path) -> pl.DataFrame:
        nonlocal changed
        result = original_read(path)
        if not changed:
            changed = True
            (generation / "late-entry").mkdir()
        return result

    monkeypatch.setattr(pl, "read_parquet", read_and_add_entry)
    with pytest.raises(RuntimeError, match="file set changed during read"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_a_linked_generation_member_is_rejected_before_it_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    original_is_symlink = Path.is_symlink

    def identify_device_days_as_link(path: Path) -> bool:
        return path.name == "device_days.parquet" or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", identify_device_days_as_link)
    with pytest.raises(RuntimeError, match="linked or outside"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_a_pinned_member_rechecks_link_and_generation_containment_on_every_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    loaded = agreement._load_annual_readiness_input(
        generation,
        expected_generation_sha256=generation_sha256,
        reviewed_geography=geography,
        config=_fixture_config(agreement, generation_sha256),
    )
    original_is_symlink = Path.is_symlink

    def identify_device_days_as_link(path: Path) -> bool:
        return path == loaded.device_days.path or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", identify_device_days_as_link)
    with (
        pytest.raises(RuntimeError, match="linked or outside"),
        agreement.stable_annual_member_path(loaded.device_days),
    ):
        pass


def test_an_incomplete_annual_generation_is_rejected(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["complete"] = False
    _write_json(manifest_path, manifest)

    with pytest.raises(RuntimeError, match="not complete"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("schema_version", 2),
        ("schema_version", 1.0),
        ("analysis", "another_analysis"),
    ],
)
def test_the_annual_manifest_has_one_fixed_schema_and_analysis(
    tmp_path: Path,
    field: str,
    changed: object,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = changed
    _write_json(manifest_path, manifest)
    generation, generation_sha256 = _rebind_generation(generation)

    with pytest.raises(RuntimeError, match="manifest contract changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_the_annual_summary_rejects_an_unknown_field(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    summary_path = generation / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["unreviewed"] = 1
    _write_json(summary_path, summary)
    generation, generation_sha256 = _rebind_generation(generation)

    with pytest.raises(RuntimeError, match="summary fields changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_claim_boundary_values_are_real_booleans_and_cannot_be_mutated() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    raw = deepcopy(load_conf("micro_sensor_annual_agreement"))
    raw["analysis"]["claim_boundary"]["validated_calibration"] = 0

    with pytest.raises(ConfigError, match="claim_boundary values must be booleans"):
        agreement.load_annual_agreement_config(raw)

    config = agreement.load_annual_agreement_config()
    boundary: Any = config.claim_boundary
    with pytest.raises(TypeError):
        boundary[0] = ("validated_calibration", True)


def test_a_missing_member_is_rejected_before_loading_starts(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    missing, missing_sha, missing_geo = _empty_annual_fixture(tmp_path / "missing")
    (missing / "calendar_coverage.parquet").unlink()
    with pytest.raises(RuntimeError, match="file set changed"):
        agreement._load_annual_readiness_input(
            missing,
            expected_generation_sha256=missing_sha,
            reviewed_geography=missing_geo,
            config=_fixture_config(agreement, missing_sha),
        )


@pytest.mark.parametrize("member", ["calendar_coverage", "device_days"])
def test_eager_and_path_backed_members_both_reject_a_wrong_schema(
    tmp_path: Path,
    member: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    wrong, _, wrong_geo = _empty_annual_fixture(tmp_path / member)
    pl.DataFrame({"wrong": [1]}).write_parquet(wrong / f"{member}.parquet")
    wrong, wrong_sha = _rebind_generation(wrong)
    with pytest.raises(RuntimeError, match=rf"schema changed: {member}\.parquet"):
        agreement._load_annual_readiness_input(
            wrong,
            expected_generation_sha256=wrong_sha,
            reviewed_geography=wrong_geo,
            config=_fixture_config(agreement, wrong_sha),
        )
