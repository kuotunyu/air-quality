from __future__ import annotations

import importlib
import json
import math
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
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
    elif ground_mutation == "short_valid":
        ground = ground.head(17)
        annual = annual.with_columns(
            pl.lit(17).alias("ground_present_trio_hours"),
            pl.lit(17).alias("ground_eligible_trio_hours"),
            pl.lit(1).alias("ground_absent_trio_hours"),
        )
    elif ground_mutation == "duplicate_timestamp":
        ground = pl.concat((ground, ground.head(1)))
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
        "catalog_generation_sha256": "a" * 64,
        "raw_observation_generation_sha256": "b" * 64,
        "parsed_generation_sha256": "c" * 64,
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


def test_checkpoint_inputs_keep_exact_distinct_catalogue_raw_observation_and_parsed_generations(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path, _ = _single_agreement_inputs(tmp_path)
    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)

    normalized = agreement._checkpoint_inputs(
        identities,
        config=agreement.load_annual_agreement_config(),
    )

    assert normalized["catalog_generation_sha256"] == "a" * 64
    assert normalized["raw_observation_generation_sha256"] == "b" * 64
    assert normalized["parsed_generation_sha256"] == "c" * 64


@pytest.mark.parametrize("mutation", ["missing", "extra", "boolean"])
def test_checkpoint_inputs_reject_missing_extra_or_boolean_source_generation_identities(
    tmp_path: Path,
    mutation: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    micro_paths, annual_path, ground_path, _ = _single_agreement_inputs(tmp_path)
    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)
    if mutation == "missing":
        del identities["raw_observation_generation_sha256"]
        message = "input identity fields changed"
    elif mutation == "extra":
        identities["unexpected_generation_sha256"] = "d" * 64
        message = "input identity fields changed"
    else:
        identities["raw_observation_generation_sha256"] = True
        message = "raw_observation_generation_sha256 changed"

    with pytest.raises(RuntimeError, match=message):
        agreement._checkpoint_inputs(
            identities,
            config=agreement.load_annual_agreement_config(),
        )


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


def test_checkpoint_validation_recomputes_the_reason_from_immutable_evidence(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    result = _aggregate_single_agreement_day(tmp_path, ground_mutation="absent")
    rebound = result.rows.with_columns(pl.lit("device_day_absent").alias("reason"))

    with pytest.raises(RuntimeError, match="checkpoint reason changed"):
        agreement._validate_agreement_day_rows(rebound, day=AGREEMENT_DAY)


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
    assert result.rows["ground_station_present_hours"].to_list() == [18]
    assert result.rows["ground_station_eligible_hours"].to_list() == [0]


def test_an_absent_ground_day_stays_distinct_from_present_but_ineligible(
    tmp_path: Path,
) -> None:
    result = _aggregate_single_agreement_day(tmp_path, ground_mutation="absent")

    assert result.rows["reason"].to_list() == ["ground_absent"]
    assert result.rows["ground_pm25_mean"].to_list() == [None]
    assert result.rows["ground_present_trio_hours"].to_list() == [0]
    assert result.rows["ground_station_present_hours"].to_list() == [0]
    assert result.rows["ground_station_eligible_hours"].to_list() == [0]


def test_a_non_finite_ground_value_is_retained_as_present_but_ineligible(
    tmp_path: Path,
) -> None:
    result = _aggregate_single_agreement_day(tmp_path, ground_mutation="non_finite")

    assert result.rows["reason"].to_list() == ["ground_present_but_ineligible"]
    assert result.rows["ground_eligible_trio_hours"].to_list() == [0]
    assert result.rows["ground_present_ineligible_trio_hours"].to_list() == [18]
    assert result.rows["ground_pm25_mean"].to_list() == [None]
    assert result.rows["ground_station_present_hours"].to_list() == [18]
    assert result.rows["ground_station_eligible_hours"].to_list() == [0]


def test_seventeen_valid_station_hours_withhold_the_canonical_target(tmp_path: Path) -> None:
    result = _aggregate_single_agreement_day(
        tmp_path,
        ground_mutation="short_valid",
    )

    assert result.rows["reason"].to_list() == ["ground_present_but_ineligible"]
    assert result.rows["ground_station_present_hours"].to_list() == [17]
    assert result.rows["ground_station_eligible_hours"].to_list() == [17]
    assert result.rows["ground_pm25_mean"].to_list() == [None]


def test_duplicate_reference_station_hours_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"ground PM2\.5 member is invalid"):
        _aggregate_single_agreement_day(tmp_path, ground_mutation="duplicate_timestamp")


def test_two_devices_at_one_station_share_the_complete_station_day_target(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    candidates = pl.DataFrame(
        {
            "device_id": ["device-a", "device-b"],
            "station_name": ["station-a", "station-a"],
            "distance_km": [0.1, 0.2],
        }
    )
    micro_paths: dict[str, Path] = {}
    values = {"pm25": 20.0, "humidity": 60.0, "temperature": 25.0}
    for variable, value in values.items():
        rows: list[dict[str, object]] = []
        row_number = 1
        for device_id, first_hour in (("device-a", 0), ("device-b", 6)):
            for hour in range(first_hour, first_hour + 18):
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
        path = tmp_path / f"{variable}.parquet"
        pl.DataFrame(rows, schema=dict(OBSERVATION_OUTPUT_SCHEMA)).write_parquet(path)
        micro_paths[variable] = path

    annual_row = _agreement_annual_days().filter(pl.col("device_id") == "eligible")
    annual = pl.concat(
        [
            annual_row.with_columns(
                pl.lit(device_id).alias("device_id"),
                pl.lit(distance_km).alias("distance_km"),
            )
            for device_id, distance_km in candidates.select("device_id", "distance_km").iter_rows()
        ]
    ).sort("device_id")
    annual_path = tmp_path / "annual-device-days.parquet"
    annual.write_parquet(annual_path)

    ground = pl.DataFrame(
        [
            {
                "station_name": "station-a",
                "pollutant": "PM2.5",
                "ts_local": datetime(2025, 1, 2, hour),
                "value": float(hour),
                "flag": "valid",
                "value_retained": True,
                "imputed": False,
                "impute_method": None,
                "generation": "fixture",
                "source_member": "fixture.csv",
            }
            for hour in range(24)
        ]
    ).select(*(pl.col(name).cast(dtype).alias(name) for name, dtype in PARTITION_SCHEMA.items()))
    ground_path = tmp_path / "ground.parquet"
    ground.write_parquet(ground_path)

    identities = _checkpoint_input_identities(micro_paths, annual_path, ground_path)
    identities["candidate_identity_sha256"] = _canonical_hash(candidates.to_dicts())
    identities["annual_selected_date_rows"] = 2
    result = agreement._aggregate_agreement_day(
        day=AGREEMENT_DAY,
        micro_paths=micro_paths,
        annual_device_days=_pinned_annual_member(agreement, annual_path),
        ground_path=ground_path,
        candidates=candidates,
        input_identities=identities,
        config=replace(
            agreement.load_annual_agreement_config(),
            primary_devices=2,
            primary_stations=1,
        ),
    )

    assert result.rows["ground_present_trio_hours"].to_list() == [18, 18]
    assert result.rows["ground_eligible_trio_hours"].to_list() == [18, 18]
    assert result.rows["ground_station_present_hours"].to_list() == [24, 24]
    assert result.rows["ground_station_eligible_hours"].to_list() == [24, 24]
    assert result.rows["ground_pm25_mean"].to_list() == [11.5, 11.5]

    changed = (
        result.rows.with_row_index()
        .with_columns(
            pl.when(pl.col("index") == 0)
            .then(pl.lit(12.5))
            .otherwise(pl.col("ground_pm25_mean"))
            .alias("ground_pm25_mean")
        )
        .drop("index")
    )
    with pytest.raises(RuntimeError, match="station-day target changed"):
        agreement._validate_agreement_day_rows(changed, day=AGREEMENT_DAY)


def _catalogue_absent_identities(
    annual_path: Path,
    candidates: pl.DataFrame,
) -> dict[str, object]:
    return {
        "catalog_generation_sha256": "a" * 64,
        "raw_observation_generation_sha256": "b" * 64,
        "parsed_generation_sha256": "c" * 64,
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
                "catalog_generation_sha256": "a" * 64,
                "raw_observation_generation_sha256": "b" * 64,
                "parsed_generation_sha256": "c" * 64,
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


@pytest.mark.parametrize(
    "identity_name",
    [
        "catalog_generation_sha256",
        "raw_observation_generation_sha256",
        "parsed_generation_sha256",
    ],
)
def test_a_checkpoint_cannot_be_reused_after_any_source_generation_identity_changes(
    tmp_path: Path,
    identity_name: str,
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
    changed[identity_name] = "e" * 64

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
            # Both ends of the pipe, pinned. The child writes per
            # PYTHONIOENCODING or the locale, `text=True` alone decodes by
            # locale, and the two do not have to agree — the child's traceback
            # carries this repository's own path, so a mismatch leaves
            # `stderr` as None and the assertion below fails on a TypeError
            # that has nothing to do with locking.
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
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

    assert config.protocol_revision == 2
    assert config.target_definition == "canonical_reference_station_day_pm25"
    assert config.ground_minimum_eligible_hours == 18
    assert config.evaluation_name == "q4_supported_cross_station_agreement"
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
        "q4_supported_cross_station_agreement": True,
        "held_station_within_observed_q4_support": True,
        "held_quarter_estimable": False,
        "joint_station_quarter_estimable": False,
        "annual_temporal_generalization": False,
        "seasonal_generalization": False,
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


def test_the_revised_protocol_fields_require_exact_values_and_types() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    raw = deepcopy(load_conf("micro_sensor_annual_agreement"))
    analysis = raw["analysis"]
    analysis.update(
        {
            "protocol_revision": 2,
            "target_definition": "canonical_reference_station_day_pm25",
            "ground_minimum_eligible_hours": 18,
            "evaluation_name": "q4_supported_cross_station_agreement",
        }
    )
    analysis["claim_boundary"].update(
        {
            "q4_supported_cross_station_agreement": True,
            "held_station_within_observed_q4_support": True,
            "held_quarter_estimable": False,
            "joint_station_quarter_estimable": False,
            "annual_temporal_generalization": False,
            "seasonal_generalization": False,
        }
    )

    reviewed = agreement.load_annual_agreement_config(raw)
    assert reviewed.protocol_revision == 2

    mutations: tuple[tuple[str, Any], ...] = (
        ("missing", None),
        ("extra", None),
        ("protocol_revision", True),
        ("ground_minimum_eligible_hours", 18.0),
        ("target_definition", "device_trio_overlap_ground_pm25"),
        ("evaluation_name", "annual_reference_station_agreement_benchmark"),
        ("held_quarter_estimable", True),
    )
    for field, value in mutations:
        changed = deepcopy(raw)
        if field == "missing":
            del changed["analysis"]["protocol_revision"]
        elif field == "extra":
            changed["analysis"]["unreviewed"] = False
        elif field in changed["analysis"]["claim_boundary"]:
            changed["analysis"]["claim_boundary"][field] = value
        else:
            changed["analysis"][field] = value
        with pytest.raises(ConfigError):
            agreement.load_annual_agreement_config(changed)


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


def _annual_agreement_panel_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, tuple[Any, ...], Any]:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    config = agreement.load_annual_agreement_config()
    reviewed_panel = agreement.load_annual_micro_sensor_panel_config()
    catalogs = dict(reviewed_panel.catalog_generations)
    parsed_generations = {
        record.date: record.generation_sha256 for record in reviewed_panel.parsed_generations
    }
    candidates = _cohort_fixture(devices=124, stations=13).select(
        "device_id", "station_name", "distance_km"
    )
    dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(365)]
    parsed = sorted(parsed_generations)
    data_root = tmp_path / "data"
    monkeypatch.setenv("TWAIR_DATA_DIR", str(data_root))
    ground_files: list[dict[str, object]] = []
    for month in range(1, 13):
        relative = Path(f"processed/observations/year=2025/month={month:02d}/part-0.parquet")
        ground_path = data_root / relative
        ground_path.parent.mkdir(parents=True)
        pl.DataFrame(schema=PARTITION_SCHEMA).write_parquet(ground_path)
        ground_files.append(
            {
                "path": relative.as_posix(),
                "bytes": ground_path.stat().st_size,
                "sha256": sha256_file(ground_path),
            }
        )
    calendar = pl.DataFrame(
        {
            "date": dates,
            "state": [
                "complete" if day in parsed_generations else "catalogue_absent" for day in dates
            ],
            "catalog_generation_sha256": [catalogs[day.strftime("%Y%m")] for day in dates],
            "parsed_generation_sha256": [parsed_generations.get(day) for day in dates],
        },
        schema=dict(ANNUAL_CALENDAR_SCHEMA),
    )
    annual_dir = tmp_path / ANNUAL_GENERATION
    annual_dir.mkdir(parents=True)
    annual_path = annual_dir / "device_days.parquet"
    pl.DataFrame(schema=dict(ANNUAL_DEVICE_DAY_SCHEMA)).write_parquet(annual_path)
    pinned = _pinned_annual_member(agreement, annual_path)
    annual_input = agreement.AnnualReadinessInput(
        generation_dir=annual_dir.resolve(),
        manifest={
            "generation_sha256": ANNUAL_GENERATION,
            "inputs": {
                "catalog_generations": catalogs,
                "parsed_generations": [
                    {
                        "date": day.isoformat(),
                        "generation_sha256": parsed_generations[day],
                        "input_files": [
                            {
                                "path": f"parsed/{day.isoformat()}/{variable}.parquet",
                                "bytes": 1,
                                "sha256": digest,
                            }
                            for variable, digest in (
                                ("pm25", "1" * 64),
                                ("humidity", "2" * 64),
                                ("temperature", "3" * 64),
                            )
                        ],
                    }
                    for index, day in enumerate(parsed)
                ],
                "ground_files": ground_files,
            },
        },
        summary={},
        calendar_coverage=calendar,
        device_days=pinned,
        device_cohorts=_cohort_fixture(devices=124, stations=13),
        cohort_thresholds=pl.DataFrame(schema=dict(ANNUAL_COHORT_THRESHOLD_SCHEMA)),
        exclusions=pl.DataFrame(schema=dict(ANNUAL_EXCLUSION_SCHEMA)),
        candidate_cohorts=tuple(
            agreement.AnnualDistanceCohort(radius_km=radius, candidates=candidates)
            for radius in config.distance_bands_km
        ),
    )
    checkpoints: list[Any] = []
    checkpoint_root = tmp_path / "checkpoints"
    candidate_identity = _canonical_hash(candidates.sort("device_id").to_dicts())
    checkpoint_config = json.loads(json.dumps(asdict(config), allow_nan=False))
    for day in parsed:
        directory = checkpoint_root / day.isoformat()
        directory.mkdir(parents=True)
        values: dict[str, list[object]] = {}
        for name, dtype in agreement.AGREEMENT_DAY_SCHEMA:
            if name == "date":
                values[name] = [day] * candidates.height
            elif name == "device_id":
                values[name] = candidates["device_id"].to_list()
            elif name == "station_name":
                values[name] = candidates["station_name"].to_list()
            elif name == "distance_km":
                values[name] = candidates["distance_km"].to_list()
            elif name == "spatial_state":
                values[name] = ["missing_pm25_coordinate"] * candidates.height
            elif name == "reason":
                values[name] = ["device_day_absent"] * candidates.height
            elif dtype == pl.Int64:
                values[name] = [0] * candidates.height
            else:
                values[name] = [None] * candidates.height
        rows = pl.DataFrame(values, schema=dict(agreement.AGREEMENT_DAY_SCHEMA))
        member_path = directory / "paired_day.parquet"
        rows.write_parquet(member_path)
        inputs = {
            "catalog_generation_sha256": catalogs[day.strftime("%Y%m")],
            "raw_observation_generation_sha256": "4" * 64,
            "parsed_generation_sha256": parsed_generations[day],
            "source_members": {
                variable: {
                    "path": f"{variable}.parquet",
                    "bytes": 1,
                    "sha256": digest,
                }
                for variable, digest in (
                    ("pm25", "1" * 64),
                    ("humidity", "2" * 64),
                    ("temperature", "3" * 64),
                )
            },
            "ground_member": {
                "path": "part-0.parquet",
                "bytes": ground_files[day.month - 1]["bytes"],
                "sha256": ground_files[day.month - 1]["sha256"],
            },
            "annual_generation_sha256": ANNUAL_GENERATION,
            "annual_device_days": {
                "path": annual_path.name,
                "bytes": annual_path.stat().st_size,
                "sha256": sha256_file(annual_path),
            },
            "candidate_identity_sha256": candidate_identity,
            "catalogue_state": "present",
            "annual_selected_date_rows": 0,
        }
        summary = {"device_day_absent": candidates.height}
        manifest = {
            "schema_version": 1,
            "kind": "q4_supported_cross_station_agreement_day",
            "date": day.isoformat(),
            "inputs": inputs,
            "config": checkpoint_config,
            "rows": candidates.height,
            "schema": {name: str(dtype) for name, dtype in agreement.AGREEMENT_DAY_SCHEMA},
            "member": {
                "path": member_path.name,
                "bytes": member_path.stat().st_size,
                "sha256": sha256_file(member_path),
            },
            "summary": summary,
            "summary_sha256": _canonical_hash(summary),
            "complete": True,
        }
        manifest_path = directory / "manifest.json"
        _write_json(manifest_path, manifest)
        checkpoints.append(
            agreement.AgreementDayCheckpoint(
                directory=directory,
                member_path=member_path,
                manifest_path=manifest_path,
                rows=rows,
                manifest=manifest,
            )
        )
    return annual_input, tuple(checkpoints), config


def _agreement_model_panel_fixture(agreement: Any) -> Any:
    rows: list[dict[str, object]] = []
    quarter_dates = (
        date(2025, 1, 15),
        date(2025, 4, 15),
        date(2025, 7, 15),
        date(2025, 10, 15),
    )
    for station_index in range(10):
        for quarter, day in enumerate(quarter_dates, start=1):
            rows.append(
                {
                    "radius_km": 0.5,
                    "date": day,
                    "device_id": f"device-{station_index:02d}",
                    "station_name": f"station-{station_index:02d}",
                    "quarter": quarter,
                    "reason": "eligible",
                    "micro_pm25_mean": float(10 + station_index + quarter),
                    "micro_humidity_mean": float(60 + quarter),
                    "micro_temperature_mean": float(20 + quarter),
                    "ground_pm25_mean": float(11 + station_index + quarter),
                }
            )
    paired_days = pl.DataFrame(rows)
    return agreement.AnnualAgreementPanel(
        calendar=pl.DataFrame(),
        paired_days=paired_days,
        exclusions=pl.DataFrame(),
        cohort_coverage=pl.DataFrame(),
        summary={},
        manifest={"generation_sha256": "a" * 64},
        annual_input=None,
        checkpoints=None,
    )


def test_joint_transfer_exposes_neither_the_test_station_fold_nor_test_quarter_to_training(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    memberships = agreement.assign_agreement_folds(panel)

    assert memberships.filter(pl.col("evaluation") == "held_station")["fold"].n_unique() == 5
    assert memberships.filter(pl.col("evaluation") == "held_quarter")["fold"].n_unique() == 4
    joint = memberships.filter(pl.col("evaluation") == "joint")
    assert joint["fold"].n_unique() == 20
    for split in joint.partition_by("fold", as_dict=False):
        train = split.filter(pl.col("role") == "train")
        test = split.filter(pl.col("role") == "test")
        assert set(train["station_fold"]).isdisjoint(set(test["station_fold"]))
        assert set(train["quarter"]).isdisjoint(set(test["quarter"]))
    test_memberships = joint.filter(pl.col("role") == "test")
    assert (
        test_memberships.select("radius_km", "date", "device_id").n_unique()
        == panel.paired_days.height
    )
    assert test_memberships.height == panel.paired_days.height
    for evaluation in ("held_station", "held_quarter"):
        tests = memberships.filter(
            (pl.col("evaluation") == evaluation) & (pl.col("role") == "test")
        )
        assert tests.height == panel.paired_days.height
        assert tests.select("radius_km", "date", "device_id").n_unique() == panel.paired_days.height


def test_fold_membership_is_deterministic_hashed_and_does_not_remove_panel_exclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    excluded = panel.paired_days.row(0, named=True)
    panel = replace(
        panel,
        paired_days=pl.concat(
            [
                panel.paired_days,
                pl.DataFrame(
                    [
                        {
                            **excluded,
                            "device_id": "excluded-device",
                            "reason": "device_day_absent",
                            "micro_pm25_mean": None,
                            "micro_humidity_mean": None,
                            "micro_temperature_mean": None,
                            "ground_pm25_mean": None,
                        }
                    ]
                ),
            ],
            how="diagonal_relaxed",
        ),
    )
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    first = agreement.assign_agreement_folds(panel)
    second = agreement.assign_agreement_folds(panel)

    assert first.rows() == second.rows()
    assert first.height == 1160
    assert dict(first.group_by("role").len().iter_rows()) == {
        "excluded": 280,
        "test": 120,
        "train": 760,
    }
    assert panel.paired_days.filter(pl.col("device_id") == "excluded-device").height == 1
    assert first.filter(pl.col("device_id") == "excluded-device").is_empty()
    per_fold = first.group_by("evaluation", "fold").agg(
        pl.col("train_membership_sha256").n_unique().alias("train_hashes"),
        pl.col("test_membership_sha256").n_unique().alias("test_hashes"),
        pl.col("test_truth_sha256").n_unique().alias("truth_hashes"),
    )
    assert per_fold.select(
        (pl.col("train_hashes") == 1).all(),
        (pl.col("test_hashes") == 1).all(),
        (pl.col("truth_hashes") == 1).all(),
    ).row(0) == (True, True, True)
    assert first.filter(
        ~pl.col("train_membership_sha256").str.contains(r"^[0-9a-f]{64}$")
        | ~pl.col("test_membership_sha256").str.contains(r"^[0-9a-f]{64}$")
        | ~pl.col("test_truth_sha256").str.contains(r"^[0-9a-f]{64}$")
    ).is_empty()
    fold_summary = (
        first.select(
            "evaluation",
            "fold",
            "fold_state",
            "train_rows",
            "test_rows",
            "train_membership_sha256",
            "test_membership_sha256",
            "test_truth_sha256",
        )
        .unique()
        .sort("evaluation", "fold")
    )
    assert _canonical_hash(fold_summary.to_dicts()) == (
        "27839354f4ad1db1efd4e8b610a33557c5ddf93471f15b476f80619fa4e9a4e6"
    )


def test_q4_only_support_persists_all_temporal_and_joint_training_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    model_columns = (
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
        "ground_pm25_mean",
    )
    panel = replace(
        panel,
        paired_days=panel.paired_days.with_columns(
            pl.when(pl.col("quarter") == 4)
            .then(pl.lit("eligible"))
            .otherwise(pl.lit("device_day_absent"))
            .alias("reason"),
            *(
                pl.when(pl.col("quarter") == 4)
                .then(pl.col(column))
                .otherwise(pl.lit(None, dtype=pl.Float64))
                .alias(column)
                for column in model_columns
            ),
        ),
    )
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    memberships = agreement.assign_agreement_folds(panel)
    folds = agreement._agreement_fold_table(memberships)

    assert folds.height == 29
    assert folds.group_by("fold_state").len().sort("fold_state").rows() == [
        ("scored", 5),
        ("unscored_empty_test", 18),
        ("unscored_empty_train", 6),
    ]
    held_q4 = folds.filter(pl.col("fold") == "held_quarter_04").row(0, named=True)
    assert held_q4["fold_state"] == "unscored_empty_train"
    assert held_q4["fold_reason"] == "unscored_empty_train"
    assert held_q4["train_rows"] == 0
    assert held_q4["train_unique_targets"] == 0
    assert held_q4["test_rows"] == 10
    assert held_q4["test_unique_targets"] == 10
    assert folds.filter(pl.col("fold_state").str.starts_with("unscored")).select(
        pl.col("test_membership_sha256").str.contains(r"^[0-9a-f]{64}$").all(),
        pl.col("test_truth_sha256").str.contains(r"^[0-9a-f]{64}$").all(),
    ).row(0) == (True, True)

    result = agreement.evaluate_annual_agreement(panel)
    assert result.manifest["analysis"] == "q4_supported_cross_station_agreement_evaluation"
    assert result.predictions.height == 30
    assert result.predictions["evaluation"].unique().to_list() == ["held_station"]
    assert result.predictions["fold_state"].unique().to_list() == ["scored"]
    assert result.scores.height == 192
    assert result.deltas.height == 128
    held_q4_scores = result.scores.filter(
        (pl.col("scope") == "fold") & (pl.col("fold") == "held_quarter_04")
    )
    assert held_q4_scores.height == 6
    assert held_q4_scores.select(
        (pl.col("state") == "unscored_empty_train").all(),
        (pl.col("n") == 0).all(),
        (pl.col("intended_n") == 10).all(),
        (pl.col("scored_membership_sha256") == _canonical_hash([])).all(),
        (pl.col("scored_truth_sha256") == _canonical_hash([])).all(),
        (pl.col("total_folds") == 1).all(),
        (pl.col("scored_folds") == 0).all(),
    ).row(0) == (True, True, True, True, True, True, True)
    overall = result.scores.filter(pl.col("scope") == "overall")
    assert overall.group_by("evaluation", "state").len().sort("evaluation").rows() == [
        ("held_quarter", "unscored_no_scored_folds", 6),
        ("held_station", "scored", 6),
        ("joint", "unscored_no_scored_folds", 6),
    ]
    unscored_overall = overall.filter(pl.col("evaluation").is_in(("held_quarter", "joint")))
    assert unscored_overall.select(
        (pl.col("n") == 0).all(),
        (pl.col("intended_n") == 10).all(),
        (pl.col("scored_folds") == 0).all(),
        (pl.col("total_folds") > 0).all(),
        pl.all_horizontal(
            pl.col(metric).is_null() for metric in ("rmse", "mae", "r2", "bias", "absolute_bias")
        ).all(),
    ).row(0) == (True, True, True, True, True)


def test_a_partially_estimable_evaluation_separates_intended_and_scored_populations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    baseline = agreement.assign_agreement_folds(panel)
    held_devices = baseline.filter(
        (pl.col("fold") == "held_station_00") & (pl.col("role") == "test")
    )["device_id"].unique()
    panel = replace(
        panel,
        paired_days=panel.paired_days.with_columns(
            pl.when(pl.col("device_id").is_in(held_devices.to_list()))
            .then(pl.lit(25.0))
            .otherwise(pl.col("ground_pm25_mean"))
            .alias("ground_pm25_mean")
        ),
    )

    result = agreement.evaluate_annual_agreement(panel)

    held_folds = result.folds.filter(pl.col("evaluation") == "held_station")
    assert held_folds.group_by("fold_state").len().sort("fold_state").rows() == [
        ("scored", 4),
        ("unscored_single_target", 1),
    ]
    overall = result.scores.filter(
        (pl.col("scope") == "overall")
        & (pl.col("evaluation") == "held_station")
        & (pl.col("model") == "raw_micro")
    )
    assert overall.height == 2
    assert overall.select(
        (pl.col("state") == "partially_scored").all(),
        (pl.col("intended_n") == 40).all(),
        (pl.col("n") == 32).all(),
        (pl.col("total_folds") == 5).all(),
        (pl.col("scored_folds") == 4).all(),
        (pl.col("membership_sha256") != pl.col("scored_membership_sha256")).all(),
        (pl.col("truth_sha256") != pl.col("scored_truth_sha256")).all(),
        pl.all_horizontal(
            pl.col(metric).is_not_null()
            for metric in ("rmse", "mae", "r2", "bias", "absolute_bias")
        ).all(),
    ).row(0) == (True, True, True, True, True, True, True, True)


def test_an_all_unscored_protocol_has_typed_empty_predictions_and_complete_null_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    panel = replace(
        panel,
        paired_days=panel.paired_days.with_columns(pl.lit(25.0).alias("ground_pm25_mean")),
    )
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    result = agreement.evaluate_annual_agreement(panel)

    assert result.folds["fold_state"].unique().to_list() == ["unscored_insufficient_train"]
    assert result.predictions.is_empty()
    assert result.predictions.schema == dict(agreement.AGREEMENT_PREDICTION_SCHEMA)
    assert result.scores.height == 192
    assert result.deltas.height == 128
    assert result.scores.filter(pl.col("scope") == "overall")["state"].unique().to_list() == [
        "unscored_no_scored_folds"
    ]
    assert result.scores.select(
        (pl.col("n") == 0).all(),
        pl.all_horizontal(
            pl.col(metric).is_null() for metric in ("rmse", "mae", "r2", "bias", "absolute_bias")
        ).all(),
    ).row(0) == (True, True)


def test_a_panel_with_no_eligible_rows_persists_all_29_empty_train_folds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    model_columns = (
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
        "ground_pm25_mean",
    )
    panel = replace(
        panel,
        paired_days=panel.paired_days.with_columns(
            pl.lit("device_day_absent").alias("reason"),
            *(pl.lit(None, dtype=pl.Float64).alias(column) for column in model_columns),
        ),
    )
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    result = agreement.evaluate_annual_agreement(panel)

    assert result.memberships.is_empty()
    assert result.folds.height == 29
    assert result.folds.select(
        (pl.col("fold_state") == "unscored_empty_train").all(),
        (pl.col("train_rows") == 0).all(),
        (pl.col("test_rows") == 0).all(),
    ).row(0) == (True, True, True)
    assert result.predictions.is_empty()
    assert result.scores.height == 192
    assert result.deltas.height == 128
    assert result.scores.select(
        (pl.col("n") == 0).all(),
        (pl.col("intended_n") == 0).all(),
        pl.all_horizontal(
            pl.col(metric).is_null() for metric in ("rmse", "mae", "r2", "bias", "absolute_bias")
        ).all(),
    ).row(0) == (True, True, True)


def test_empty_and_single_target_test_folds_remain_explicit_and_unscored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    panel = replace(
        panel,
        paired_days=panel.paired_days.with_columns(
            pl.when(pl.col("quarter") == 4)
            .then(pl.lit("device_day_absent"))
            .otherwise(pl.col("reason"))
            .alias("reason"),
            pl.when(pl.col("quarter") == 1)
            .then(pl.lit(25.0))
            .otherwise(pl.col("ground_pm25_mean"))
            .alias("ground_pm25_mean"),
        ),
    )
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    memberships = agreement.assign_agreement_folds(panel)
    result = agreement.evaluate_annual_agreement(panel)

    empty = memberships.filter(pl.col("fold") == "held_quarter_04")
    assert empty["fold_state"].unique().to_list() == ["unscored_empty_test"]
    assert empty["test_rows"].unique().to_list() == [0]
    single = memberships.filter(pl.col("fold") == "joint_00_01")
    assert single["fold_state"].unique().to_list() == ["unscored_single_target"]
    empty_folds = result.folds.filter(pl.col("fold_state") == "unscored_empty_test")[
        "fold"
    ].to_list()
    assert empty_folds == [
        "held_quarter_04",
        "joint_00_04",
        "joint_01_04",
        "joint_02_04",
        "joint_03_04",
        "joint_04_04",
    ]
    assert result.predictions.filter(pl.col("fold").is_in(empty_folds)).is_empty()
    empty_scores = result.scores.filter(pl.col("fold").is_in(empty_folds))
    empty_deltas = result.deltas.filter(pl.col("fold").is_in(empty_folds))
    assert empty_scores.height == len(empty_folds) * 3 * 2
    assert empty_deltas.height == len(empty_folds) * 2 * 2
    assert empty_scores.select(
        (pl.col("state") == "unscored_empty_test").all(),
        (pl.col("n") == 0).all(),
        pl.all_horizontal(
            pl.col(metric).is_null() for metric in ("rmse", "mae", "r2", "bias", "absolute_bias")
        ).all(),
    ).row(0) == (True, True, True)
    assert empty_deltas.select(
        (pl.col("state") == "unscored_empty_test").all(),
        (pl.col("n") == 0).all(),
        pl.all_horizontal(
            pl.col(column).is_null()
            for column in empty_deltas.columns
            if column.startswith("delta_")
        ).all(),
    ).row(0) == (True, True, True)


def test_station_fold_identity_uses_the_full_panel_universe_when_a_station_has_no_eligible_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    reduced = replace(
        panel,
        paired_days=panel.paired_days.with_columns(
            pl.when(pl.col("station_name") == "station-00")
            .then(pl.lit("device_day_absent"))
            .otherwise(pl.col("reason"))
            .alias("reason")
        ),
    )

    full = agreement.assign_agreement_folds(panel)
    changed = agreement.assign_agreement_folds(reduced)
    full_assignments = full.select("station_name", "station_fold").unique().sort("station_name")
    changed_assignments = (
        changed.select("station_name", "station_fold").unique().sort("station_name")
    )

    assert (
        changed_assignments.rows()
        == full_assignments.filter(pl.col("station_name") != "station-00").rows()
    )


def test_fold_assignment_rejects_invalid_eligible_membership_before_modeling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    invalid_quarter = replace(
        panel,
        paired_days=panel.paired_days.with_columns(
            pl.when(pl.col("device_id") == "device-00")
            .then(pl.lit(5, dtype=pl.Int64))
            .otherwise(pl.col("quarter"))
            .alias("quarter")
        ),
    )

    with pytest.raises(RuntimeError, match="eligible quarter"):
        agreement.assign_agreement_folds(invalid_quarter)


def test_the_fixed_cpu_models_predict_every_and_only_held_out_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    result = agreement.evaluate_annual_agreement(panel)

    assert result.folds.height == 29
    assert result.predictions.height == 360
    assert set(result.predictions["model"]) == {
        "raw_micro",
        "pooled_micro_ridge",
        "pooled_weather_ridge",
    }
    assert set(result.predictions["model_features"]) == {
        "micro_pm25_mean",
        "micro_pm25_mean,micro_humidity_mean,micro_temperature_mean",
    }
    test_rows = result.memberships.filter(pl.col("role") == "test").select(
        "evaluation", "fold", "radius_km", "date", "device_id"
    )
    for model in result.predictions["model"].unique():
        predicted = result.predictions.filter(pl.col("model") == model).select(
            "evaluation", "fold", "radius_km", "date", "device_id"
        )
        assert predicted.rows() == test_rows.rows()
    raw = result.predictions.filter(pl.col("model") == "raw_micro").join(
        panel.paired_days.select("radius_km", "date", "device_id", "micro_pm25_mean"),
        on=["radius_km", "date", "device_id"],
        how="left",
    )
    assert raw["y_pred"].to_list() == raw["micro_pm25_mean"].to_list()


def test_each_fold_records_train_test_exclusion_and_overlap_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    result = agreement.evaluate_annual_agreement(panel)

    for fold in result.folds.iter_rows(named=True):
        membership = result.memberships.filter(pl.col("fold") == fold["fold"])
        train = membership.filter(pl.col("role") == "train")
        test = membership.filter(pl.col("role") == "test")
        assert fold["train_stations"] == train["station_name"].n_unique()
        assert fold["test_stations"] == test["station_name"].n_unique()
        assert fold["train_dates"] == train["date"].n_unique()
        assert fold["test_dates"] == test["date"].n_unique()
        assert fold["excluded_rows"] == membership.filter(pl.col("role") == "excluded").height
        assert fold["device_overlap"] == len(set(train["device_id"]) & set(test["device_id"]))


def test_ridge_preprocessing_and_inverse_station_day_weights_are_fit_only_on_training_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    duplicate = {
        **panel.paired_days.row(0, named=True),
        "device_id": "device-00-duplicate",
    }
    panel = replace(
        panel,
        paired_days=pl.concat(
            [panel.paired_days, pl.DataFrame([duplicate])], how="diagonal_relaxed"
        ),
    )
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    fit_calls: list[dict[str, Any]] = []
    thread_limits: list[int] = []
    original_fit = agreement.Pipeline.fit

    def recording_fit(self: Any, x: Any, y: Any, **kwargs: Any) -> Any:
        fit_calls.append(
            {
                "steps": tuple(name for name, _ in self.steps),
                "alpha": self.named_steps["ridge"].alpha,
                "shape": x.shape,
                "truth": y.copy(),
                "kwargs": {key: value.copy() for key, value in kwargs.items()},
            }
        )
        return original_fit(self, x, y, **kwargs)

    @contextmanager
    def recording_thread_limit(*, limits: int) -> Any:
        thread_limits.append(limits)
        yield

    monkeypatch.setattr(agreement.Pipeline, "fit", recording_fit)
    monkeypatch.setattr(agreement, "threadpool_limits", recording_thread_limit)

    agreement.evaluate_annual_agreement(panel)

    assert len(fit_calls) == 58
    assert thread_limits == [1] * 58
    assert {call["steps"] for call in fit_calls} == {("standardscaler", "ridge")}
    assert {call["alpha"] for call in fit_calls} == {1.0}
    assert {call["shape"][1] for call in fit_calls} == {1, 3}
    for call in fit_calls:
        kwargs = call["kwargs"]
        assert set(kwargs) == {
            "standardscaler__sample_weight",
            "ridge__sample_weight",
        }
        assert (
            kwargs["standardscaler__sample_weight"].tolist()
            == kwargs["ridge__sample_weight"].tolist()
        )
    assert any(0.5 in call["kwargs"]["ridge__sample_weight"].tolist() for call in fit_calls)


def test_scores_recompute_both_units_and_pair_deltas_on_identical_truth_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    evaluation = agreement.evaluate_annual_agreement(panel)
    predictions = evaluation.predictions

    scores, deltas = agreement.score_annual_agreement_predictions(
        panel,
        predictions,
    )

    assert scores.height == 192
    assert deltas.height == 128
    overall = scores.filter(pl.col("scope") == "overall")
    assert overall.select("evaluation", "model", "unit").n_unique() == 18
    selected_predictions = predictions.filter(
        (pl.col("evaluation") == "joint") & (pl.col("model") == "pooled_weather_ridge")
    )
    errors = [
        predicted - truth
        for truth, predicted in selected_predictions.select("y_true", "y_pred").iter_rows()
    ]
    truth = selected_predictions["y_true"].to_list()
    truth_mean = sum(truth) / len(truth)
    expected = {
        "rmse": math.sqrt(sum(error * error for error in errors) / len(errors)),
        "mae": sum(abs(error) for error in errors) / len(errors),
        "r2": 1
        - sum(error * error for error in errors)
        / sum((value - truth_mean) ** 2 for value in truth),
        "bias": sum(errors) / len(errors),
    }
    selected_score = overall.filter(
        (pl.col("evaluation") == "joint")
        & (pl.col("model") == "pooled_weather_ridge")
        & (pl.col("unit") == "device_day")
    ).row(0, named=True)
    for metric, value in expected.items():
        assert selected_score[metric] == pytest.approx(value)
    assert selected_score["absolute_bias"] == pytest.approx(abs(expected["bias"]))
    paired = deltas.filter(pl.col("state") == "scored")
    assert paired.filter(
        pl.col("membership_sha256").is_null() | pl.col("truth_sha256").is_null()
    ).is_empty()


def test_a_held_out_target_change_cannot_enter_that_folds_ridge_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    mutated = replace(
        panel,
        paired_days=panel.paired_days.with_columns(
            pl.when(pl.col("station_name").is_in(["station-00", "station-05"]))
            .then(pl.col("ground_pm25_mean") + 1000.0)
            .otherwise(pl.col("ground_pm25_mean"))
            .alias("ground_pm25_mean")
        ),
    )

    original = agreement.evaluate_annual_agreement(panel).predictions.filter(
        (pl.col("fold") == "held_station_00") & (pl.col("model") != "raw_micro")
    )
    changed = agreement.evaluate_annual_agreement(mutated).predictions.filter(
        (pl.col("fold") == "held_station_00") & (pl.col("model") != "raw_micro")
    )

    assert (
        original.select("model", "date", "device_id", "y_pred").rows()
        == changed.select("model", "date", "device_id", "y_pred").rows()
    )
    assert original["y_true"].to_list() != changed["y_true"].to_list()


def test_scoring_rejects_nonfinite_unpaired_missing_and_duplicate_predictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    evaluation = agreement.evaluate_annual_agreement(panel)
    predictions = evaluation.predictions
    nonfinite = (
        predictions.with_row_index()
        .with_columns(
            pl.when(pl.col("index") == 0)
            .then(pl.lit(float("inf")))
            .otherwise(pl.col("y_pred"))
            .alias("y_pred")
        )
        .drop("index")
    )
    unpaired = (
        predictions.with_row_index()
        .with_columns(
            pl.when((pl.col("index") == 0) & (pl.col("model") == "pooled_micro_ridge"))
            .then(pl.col("y_true") + 1.0)
            .otherwise(pl.col("y_true"))
            .alias("y_true")
        )
        .drop("index")
    )
    missing = predictions.slice(1)
    duplicated = pl.concat([predictions, predictions.head(1)])
    wrong_features = predictions.with_columns(
        pl.when(pl.col("model") == "raw_micro")
        .then(pl.lit("micro_humidity_mean"))
        .otherwise(pl.col("model_features"))
        .alias("model_features")
    )
    wrong_train_membership = (
        predictions.with_row_index()
        .with_columns(
            pl.when((pl.col("index") == 0) & (pl.col("model") == "pooled_micro_ridge"))
            .then(pl.lit("f" * 64))
            .otherwise(pl.col("train_membership_sha256"))
            .alias("train_membership_sha256")
        )
        .drop("index")
    )

    for changed in (
        nonfinite,
        unpaired,
        missing,
        duplicated,
        wrong_features,
        wrong_train_membership,
    ):
        with pytest.raises(RuntimeError):
            agreement.score_annual_agreement_predictions(
                panel,
                changed,
            )


def test_scoring_rejects_coordinated_missing_and_extra_rows_against_trusted_fold_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    evaluation = agreement.evaluate_annual_agreement(panel)
    predictions = evaluation.predictions
    identity = (
        predictions.filter(
            (pl.col("evaluation") == "held_quarter") & (pl.col("fold") == "held_quarter_01")
        )
        .select("radius_km", "date", "device_id")
        .row(0)
    )
    removed = predictions.filter(
        ~(
            (pl.col("evaluation") == "held_quarter")
            & (pl.col("fold") == "held_quarter_01")
            & (pl.col("radius_km") == identity[0])
            & (pl.col("date") == identity[1])
            & (pl.col("device_id") == identity[2])
        )
    )
    fabricated = (
        predictions.filter(
            (pl.col("evaluation") == "held_quarter") & (pl.col("fold") == "held_quarter_01")
        )
        .head(3)
        .with_columns(pl.lit("fabricated-device").alias("device_id"))
    )
    extra = pl.concat([predictions, fabricated])

    for changed in (removed, extra):
        with pytest.raises(RuntimeError, match="trusted fold"):
            agreement.score_annual_agreement_predictions(
                panel,
                changed,
            )


def test_scoring_rejects_coordinated_three_model_rows_outside_the_trusted_fold_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    predictions = agreement.evaluate_annual_agreement(panel).predictions
    trusted = predictions.filter(
        (pl.col("evaluation") == "held_quarter") & (pl.col("fold") == "held_quarter_01")
    )
    identity = trusted.select("radius_km", "date", "device_id").row(0)
    coordinated = trusted.filter(
        (pl.col("radius_km") == identity[0])
        & (pl.col("date") == identity[1])
        & (pl.col("device_id") == identity[2])
    )
    assert coordinated.height == 3
    assert coordinated["model"].n_unique() == 3
    fabricated_rows = (
        coordinated.with_columns(pl.lit("fabricated_fold").alias("fold")),
        coordinated.with_columns(
            pl.lit("fabricated_evaluation").alias("evaluation"),
            pl.lit("fabricated_fold").alias("fold"),
        ),
    )

    for fabricated in fabricated_rows:
        with pytest.raises(RuntimeError, match="trusted prediction universe"):
            agreement.score_annual_agreement_predictions(
                panel,
                pl.concat([predictions, fabricated]),
            )


def test_public_scoring_rejects_a_coherently_rebound_universe_that_does_not_match_the_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)
    evaluation = agreement.evaluate_annual_agreement(panel)
    identity = (
        evaluation.predictions.filter(
            (pl.col("evaluation") == "held_quarter") & (pl.col("fold") == "held_quarter_01")
        )
        .select("radius_km", "date", "device_id")
        .row(0)
    )
    rebound = evaluation.predictions.filter(
        ~(
            (pl.col("evaluation") == "held_quarter")
            & (pl.col("fold") == "held_quarter_01")
            & (pl.col("radius_km") == identity[0])
            & (pl.col("date") == identity[1])
            & (pl.col("device_id") == identity[2])
        )
    )

    with pytest.raises(RuntimeError, match="trusted fold"):
        agreement.score_annual_agreement_predictions(panel, rebound)


def test_evaluation_identity_binds_the_trusted_panel_claim_boundary_and_ordered_output_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    result = agreement.evaluate_annual_agreement(panel)
    manifest: dict[str, Any] = dict(result.manifest)
    outputs = {
        "folds": result.folds,
        "predictions": result.predictions,
        "scores": result.scores,
        "deltas": result.deltas,
    }

    assert manifest["panel_generation_sha256"] == "a" * 64
    assert manifest["claim_boundary"] == dict(
        agreement.load_annual_agreement_config().claim_boundary
    )
    assert list(manifest["output_rows"]) == list(outputs)
    assert manifest["output_rows"] == {name: frame.height for name, frame in outputs.items()}

    # `folds` is the only output with no floating-point column, and it is the
    # only one of the four whose digest can be pinned here.
    #
    # The other three carry model output, and `_agreement_frame_hash` digests
    # the full-precision repr of every float64, so one unit in the last place
    # rewrites the digest completely. That is not hypothetical. Forcing a
    # different OpenBLAS kernel on one machine — same code, same input, same
    # process — moves `pooled_weather_ridge`'s first prediction by exactly one
    # ULP (0x1.85099ac5c5952p+3 against 0x1.85099ac5c5951p+3) and rewrites
    # `predictions`, `scores` and `deltas`. Measured on Windows and on Linux,
    # same CPU: both agree bit-for-bit with each other, and both move under
    # OPENBLAS_CORETYPE=NEHALEM and =SANDYBRIDGE. `folds` held across all of it.
    #
    # `ubuntu-latest` is a pool and nothing guarantees two runs get the same host
    # CPU, so pinning the three float digests asserted which machine CI got:
    # it passed at 06:42 and 11:00 and failed at 14:32 on identical code. What
    # this test is named for — that the identity binds its components — is
    # checked below without depending on the host's kernel selection.
    assert (
        manifest["output_hashes"]["folds"]
        == "af6613bdde7beb85cf02b0ab2074722ea57464f70d82a1f2d358431db5cafbfc"
    )
    assert list(manifest["output_hashes"]) == list(outputs)
    assert manifest["output_hashes"] == {
        name: agreement._agreement_frame_hash(frame) for name, frame in outputs.items()
    }

    # Binding, stated as the property rather than as one machine's digest: the
    # generation identity is the hash of everything above it, so perturbing any
    # single component has to move it.
    identity = {key: value for key, value in manifest.items() if key != "generation_sha256"}
    assert agreement._canonical_hash(identity) == manifest["generation_sha256"]
    for mutation in (
        {"panel_generation_sha256": "b" * 64},
        {"claim_boundary": {**manifest["claim_boundary"], "invented_claim": True}},
        {"config": {**manifest["config"], "ridge_alpha": 2.0}},
        {"output_rows": {**manifest["output_rows"], "folds": 0}},
        {"output_hashes": {**manifest["output_hashes"], "predictions": "c" * 64}},
    ):
        assert agreement._canonical_hash({**identity, **mutation}) != manifest["generation_sha256"]

    # A model change is what the three float digests were really catching, so it
    # is still caught here — at a tolerance four orders of magnitude wider than
    # the one-ULP dispatch difference and far tighter than any change a model
    # would make.
    overall = result.scores.filter(
        (pl.col("scope") == "overall") & (pl.col("unit") == "station_day")
    )

    def measured(evaluation: str, model: str, column: str) -> float:
        selected = overall.filter((pl.col("evaluation") == evaluation) & (pl.col("model") == model))
        assert selected.height == 1
        return float(selected.row(0, named=True)[column])

    assert measured("joint", "raw_micro", "rmse") == pytest.approx(1.0, rel=1e-12)
    assert measured("joint", "pooled_micro_ridge", "rmse") == pytest.approx(
        0.1362187782780167, rel=1e-12
    )
    assert measured("joint", "pooled_weather_ridge", "rmse") == pytest.approx(
        0.13858150678284598, rel=1e-12
    )
    assert measured("held_station", "pooled_micro_ridge", "r2") == pytest.approx(
        0.9989729834227442, rel=1e-12
    )
    assert measured("held_station", "pooled_weather_ridge", "r2") == pytest.approx(
        0.9988032348512023, rel=1e-12
    )
    assert measured("held_quarter", "pooled_micro_ridge", "mae") == pytest.approx(
        0.08763440860215077, rel=1e-12
    )
    assert measured("held_quarter", "pooled_weather_ridge", "mae") == pytest.approx(
        0.09090669726243772, rel=1e-12
    )

    assert result.folds.rows() == result.folds.sort("evaluation", "fold").rows()
    assert (
        result.predictions.rows()
        == result.predictions.sort(
            "evaluation", "fold", "radius_km", "date", "device_id", "model"
        ).rows()
    )


def test_station_day_scores_equal_weight_targets_instead_of_dense_device_clusters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    panel = _agreement_model_panel_fixture(agreement)
    duplicate = {
        **panel.paired_days.row(0, named=True),
        "device_id": "dense-cluster-device",
        "micro_pm25_mean": 100.0,
    }
    panel = replace(
        panel,
        paired_days=pl.concat(
            [panel.paired_days, pl.DataFrame([duplicate])], how="diagonal_relaxed"
        ),
    )
    geography = pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(10)],
            "airzone_official": ["north"] * 5 + ["south"] * 5,
        }
    )
    monkeypatch.setattr(agreement, "_validate_agreement_panel_frames", lambda _: None)
    monkeypatch.setattr(agreement, "resolve_station_geo", lambda: geography)

    result = agreement.evaluate_annual_agreement(panel)
    raw = result.predictions.filter(
        (pl.col("evaluation") == "joint") & (pl.col("model") == "raw_micro")
    )
    station_days = raw.group_by("station_name", "date").agg(
        pl.col("y_true").first(), pl.col("y_pred").mean()
    )
    errors = [
        predicted - truth
        for truth, predicted in station_days.select("y_true", "y_pred").iter_rows()
    ]
    expected_rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    station_score = result.scores.filter(
        (pl.col("scope") == "overall")
        & (pl.col("evaluation") == "joint")
        & (pl.col("model") == "raw_micro")
        & (pl.col("unit") == "station_day")
    ).row(0, named=True)
    device_score = result.scores.filter(
        (pl.col("scope") == "overall")
        & (pl.col("evaluation") == "joint")
        & (pl.col("model") == "raw_micro")
        & (pl.col("unit") == "device_day")
    ).row(0, named=True)

    assert station_score["n"] == 40
    assert device_score["n"] == 41
    assert station_score["rmse"] == pytest.approx(expected_rmse)
    assert station_score["rmse"] != pytest.approx(device_score["rmse"])


def test_the_panel_contains_every_primary_device_on_all_365_dates_without_repairing_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)

    panel = agreement.prepare_annual_agreement_panel(
        annual_input,
        checkpoints,
        config,
    )

    primary = panel.paired_days.filter(pl.col("radius_km") == 0.5)
    absent = primary.filter(pl.col("calendar_state") == "catalogue_absent")
    assert primary.height == 365 * 124
    assert primary["date"].n_unique() == 365
    assert primary["device_id"].n_unique() == 124
    assert absent.height == 43 * 124
    assert absent["micro_pm25_mean"].null_count() == absent.height
    assert absent["reason"].unique().to_list() == ["catalogue_absent"]


def test_a_physically_reordered_checkpoint_key_is_rejected_even_when_its_hash_is_rebound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(tmp_path, monkeypatch)
    first = checkpoints[0]
    pl.read_parquet(first.member_path).reverse().write_parquet(first.member_path)
    changed_manifest = deepcopy(first.manifest)
    changed_manifest["member"] = {
        "path": first.member_path.name,
        "bytes": first.member_path.stat().st_size,
        "sha256": sha256_file(first.member_path),
    }
    _write_json(first.manifest_path, changed_manifest)
    changed = replace(first, manifest=changed_manifest)
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)

    with pytest.raises(RuntimeError, match="physical checkpoint key order changed"):
        agreement.prepare_annual_agreement_panel(
            annual_input,
            (changed, *checkpoints[1:]),
            config,
        )


def test_the_annual_reducer_revalidates_the_task_2_null_contract_inside_duckdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(tmp_path, monkeypatch)
    first = checkpoints[0]
    rows = pl.read_parquet(first.member_path).with_columns(pl.lit("eligible").alias("reason"))
    rows.write_parquet(first.member_path)
    changed_manifest = deepcopy(first.manifest)
    changed_manifest["member"] = {
        "path": first.member_path.name,
        "bytes": first.member_path.stat().st_size,
        "sha256": sha256_file(first.member_path),
    }
    changed_manifest["summary"] = {"eligible": rows.height}
    changed_manifest["summary_sha256"] = _canonical_hash(changed_manifest["summary"])
    _write_json(first.manifest_path, changed_manifest)
    changed = replace(first, manifest=changed_manifest)
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)

    with pytest.raises(RuntimeError, match="checkpoint eligible value is null"):
        agreement.prepare_annual_agreement_panel(
            annual_input,
            (changed, *checkpoints[1:]),
            config,
        )


@pytest.mark.parametrize(
    "mutation",
    ["catalog_generation", "annual_device_days"],
)
def test_rebound_task_2_evidence_must_still_match_the_trusted_task_1_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(tmp_path, monkeypatch)
    first = checkpoints[0]
    changed_manifest = deepcopy(first.manifest)
    if mutation == "catalog_generation":
        changed_manifest["inputs"]["catalog_generation_sha256"] = "e" * 64
    else:
        changed_manifest["inputs"]["annual_device_days"] = {
            "path": "device_days.parquet",
            "bytes": 1,
            "sha256": "e" * 64,
        }
    _write_json(first.manifest_path, changed_manifest)
    changed = replace(first, manifest=changed_manifest)
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)

    with pytest.raises(RuntimeError, match="does not match reviewed annual input"):
        agreement.prepare_annual_agreement_panel(
            annual_input,
            (changed, *checkpoints[1:]),
            config,
        )


def test_swapped_catalogue_and_raw_observation_checkpoint_identities_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(tmp_path, monkeypatch)
    first = checkpoints[0]
    changed_manifest = deepcopy(first.manifest)
    inputs = changed_manifest["inputs"]
    inputs["catalog_generation_sha256"], inputs["raw_observation_generation_sha256"] = (
        inputs["raw_observation_generation_sha256"],
        inputs["catalog_generation_sha256"],
    )
    _write_json(first.manifest_path, changed_manifest)
    changed = replace(first, manifest=changed_manifest)
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)

    with pytest.raises(RuntimeError, match="does not match reviewed annual input"):
        agreement.prepare_annual_agreement_panel(
            annual_input,
            (changed, *checkpoints[1:]),
            config,
        )


def test_the_panel_writer_persists_four_ordered_members_and_reloads_the_final_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"

    published = agreement.write_annual_agreement_panel(panel, destination=destination)
    loaded = agreement.load_annual_agreement_panel(destination)

    assert published.manifest == loaded.manifest
    assert published.manifest is not panel.manifest
    assert published.manifest["complete"] is True
    assert len(published.manifest["generation_sha256"]) == 64
    assert {path.name for path in destination.iterdir()} == {
        "calendar.parquet",
        "paired_days.parquet",
        "exclusions.parquet",
        "cohort_coverage.parquet",
        "summary.json",
        "manifest.json",
    }
    assert loaded.paired_days.select("radius_km", "date", "device_id").rows() == sorted(
        loaded.paired_days.select("radius_km", "date", "device_id").rows()
    )


def test_the_annual_reduction_scans_checkpoint_paths_without_polars_collecting_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    monkeypatch.setattr(
        agreement.pl,
        "read_parquet",
        lambda *_args, **_kwargs: pytest.fail("annual reduction used pl.read_parquet"),
    )
    monkeypatch.setattr(
        agreement.pl,
        "concat",
        lambda *_args, **_kwargs: pytest.fail("annual reduction used pl.concat"),
    )

    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)

    assert panel.paired_days.height == 365 * 124


@pytest.mark.parametrize("mutation", ["missing", "reordered"])
def test_missing_or_reordered_checkpoint_evidence_cannot_define_the_annual_panel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    changed = (
        checkpoints[1:]
        if mutation == "missing"
        else (checkpoints[1], checkpoints[0], *checkpoints[2:])
    )

    with pytest.raises(RuntimeError, match="ordered checkpoint inventory changed"):
        agreement.prepare_annual_agreement_panel(annual_input, tuple(changed), config)


def test_a_calendar_with_the_right_dates_but_the_wrong_2025_arithmetic_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(tmp_path, monkeypatch)
    calendar = (
        annual_input.calendar_coverage.with_row_index()
        .with_columns(
            pl.when(pl.col("index") == 364)
            .then(pl.lit(date(2026, 1, 1)))
            .otherwise(pl.col("date"))
            .alias("date")
        )
        .drop("index")
    )
    changed = replace(annual_input, calendar_coverage=calendar)
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: changed)

    with pytest.raises(RuntimeError, match="calendar arithmetic changed"):
        agreement.prepare_annual_agreement_panel(changed, checkpoints, config)


def test_published_member_mutation_is_rejected_even_when_row_count_and_schema_stay_equal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    member = destination / "paired_days.parquet"
    pl.read_parquet(member).reverse().write_parquet(member)

    with pytest.raises(RuntimeError, match="paired_days member changed"):
        agreement.load_annual_agreement_panel(destination)


def _rebind_agreement_panel_manifest(destination: Path) -> None:
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in ("calendar", "paired_days", "exclusions", "cohort_coverage"):
        member = destination / f"{name}.parquet"
        manifest["members"][name] = {
            "path": member.name,
            "bytes": member.stat().st_size,
            "sha256": sha256_file(member),
        }
    summary_path = destination / "summary.json"
    manifest["summary_file"] = {
        "path": summary_path.name,
        "bytes": summary_path.stat().st_size,
        "sha256": sha256_file(summary_path),
    }
    identity = {
        field: manifest[field]
        for field in (
            "schema_version",
            "analysis",
            "inputs",
            "checkpoint_inventory",
            "config",
            "claim_boundary",
            "output_rows",
            "members",
            "summary_file",
            "summary_sha256",
        )
    }
    manifest["generation_sha256"] = _canonical_hash(identity)
    _write_json(manifest_path, manifest)


def test_public_panel_loading_recomputes_station_day_counts_from_reviewed_ground_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    member = destination / "paired_days.parquet"
    rows = pl.read_parquet(member)
    first_complete = rows.filter(pl.col("calendar_state") == "complete").row(0, named=True)
    rows.with_columns(
        pl.when(
            (pl.col("date") == first_complete["date"])
            & (pl.col("station_name") == first_complete["station_name"])
        )
        .then(pl.lit(1, dtype=pl.Int64))
        .otherwise(pl.col("ground_station_present_hours"))
        .alias("ground_station_present_hours")
    ).write_parquet(member)
    _rebind_agreement_panel_manifest(destination)

    with pytest.raises(RuntimeError, match="persisted station-day target changed"):
        agreement.load_annual_agreement_panel(destination)


def test_station_day_reconstruction_repeats_ground_containment_after_duckdb_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    real_assert = agreement._assert_reviewed_direct_child
    calls = 0

    def reject_after_the_first_full_inventory(
        path: Path,
        *,
        parent: Path,
        is_directory: bool,
    ) -> None:
        nonlocal calls
        real_assert(path, parent=parent, is_directory=is_directory)
        calls += 1
        if calls == 61:
            raise RuntimeError("annual agreement reviewed source is linked or outside")

    monkeypatch.setattr(
        agreement,
        "_assert_reviewed_direct_child",
        reject_after_the_first_full_inventory,
    )

    with pytest.raises(RuntimeError, match="reviewed source is linked or outside"):
        agreement._validate_persisted_station_day_targets(
            panel,
            readiness=annual_input,
            config=config,
        )
    assert calls == 61


@pytest.mark.parametrize("mutation", ["config", "summary", "calendar", "coverage"])
def test_rebound_persisted_metadata_and_aggregates_are_not_accepted_as_a_new_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "config":
        manifest["config"]["threads"] = 999
        _write_json(manifest_path, manifest)
    elif mutation == "summary":
        summary_path = destination / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["reason_counts"]["invented"] = 999
        _write_json(summary_path, summary)
        manifest["summary_sha256"] = _canonical_hash(summary)
        _write_json(manifest_path, manifest)
    elif mutation == "calendar":
        member = destination / "calendar.parquet"
        calendar = (
            pl.read_parquet(member)
            .with_row_index()
            .with_columns(
                pl.when(pl.col("index") == 0)
                .then(pl.lit("e" * 64))
                .otherwise(pl.col("catalog_generation_sha256"))
                .alias("catalog_generation_sha256")
            )
            .drop("index")
        )
        calendar.write_parquet(member)
    else:
        member = destination / "cohort_coverage.parquet"
        coverage = (
            pl.read_parquet(member)
            .with_row_index()
            .with_columns(
                pl.when(pl.col("index") == 0)
                .then(pl.col("device_days") + 1)
                .otherwise(pl.col("device_days"))
                .alias("device_days")
            )
            .drop("index")
        )
        coverage.write_parquet(member)
    _rebind_agreement_panel_manifest(destination)

    with pytest.raises(RuntimeError, match="persisted semantics changed"):
        agreement.load_annual_agreement_panel(destination)


def test_coordinated_catalogue_calendar_and_checkpoint_rebinding_cannot_replace_task_1_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    calendar_path = destination / "calendar.parquet"
    pl.read_parquet(calendar_path).with_columns(
        pl.lit("e" * 64).alias("catalog_generation_sha256")
    ).write_parquet(calendar_path)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["catalog_generations"] = dict.fromkeys(
        manifest["inputs"]["catalog_generations"], "e" * 64
    )
    for evidence in manifest["checkpoint_inventory"]:
        evidence["catalog_generation_sha256"] = "e" * 64
    _write_json(manifest_path, manifest)
    _rebind_agreement_panel_manifest(destination)

    with pytest.raises(RuntimeError, match="persisted semantics changed"):
        agreement.load_annual_agreement_panel(destination)


def test_coordinated_candidate_and_member_rebinding_cannot_replace_the_reviewed_primary_cohort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    for name in ("paired_days", "exclusions"):
        member = destination / f"{name}.parquet"
        pl.read_parquet(member).with_columns(
            pl.concat_str(pl.lit("rebound-"), pl.col("device_id")).alias("device_id")
        ).write_parquet(member)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rebound_candidates = (
        pl.read_parquet(destination / "paired_days.parquet")
        .select("device_id", "station_name", "distance_km")
        .unique()
        .sort("device_id")
    )
    manifest["inputs"]["candidate_identity_sha256"] = _canonical_hash(rebound_candidates.to_dicts())
    _write_json(manifest_path, manifest)
    _rebind_agreement_panel_manifest(destination)

    with pytest.raises(RuntimeError, match="persisted semantics changed"):
        agreement.load_annual_agreement_panel(destination)


def test_a_checkpoint_directory_cannot_be_a_junction_to_an_outside_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "win32":
        pytest.skip("junction mutation is Windows-specific")
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(tmp_path, monkeypatch)
    first = checkpoints[0]
    outside = tmp_path / "outside-checkpoint"
    first.directory.replace(outside)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(first.directory), str(outside)],
        check=True,
        capture_output=True,
        text=True,
    )
    changed = replace(
        first,
        manifest_path=first.directory / "manifest.json",
        member_path=first.directory / "paired_day.parquet",
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)

    with pytest.raises(RuntimeError, match="linked or outside checkpoint root"):
        agreement.prepare_annual_agreement_panel(
            annual_input,
            (changed, *checkpoints[1:]),
            config,
        )


def test_the_public_loader_rejects_a_generation_junction_to_an_outside_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "win32":
        pytest.skip("junction mutation is Windows-specific")
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    outside = tmp_path / "outside-published"
    destination.replace(outside)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(destination), str(outside)],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(RuntimeError, match="linked or outside generation"):
        agreement.load_annual_agreement_panel(destination)


@pytest.mark.parametrize(
    "linked_name",
    [
        "manifest.json",
        "summary.json",
        "calendar.parquet",
        "paired_days.parquet",
        "exclusions.parquet",
        "cohort_coverage.parquet",
    ],
)
def test_the_public_loader_rejects_every_linked_persisted_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_name: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    original_is_link_like = agreement._is_link_like
    monkeypatch.setattr(
        agreement,
        "_is_link_like",
        lambda path: path.name == linked_name or original_is_link_like(path),
    )

    with pytest.raises(RuntimeError, match="linked or outside generation"):
        agreement.load_annual_agreement_panel(destination)


def test_a_rebound_output_cannot_put_a_value_on_an_explicitly_excluded_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    member = destination / "paired_days.parquet"
    rows = (
        pl.read_parquet(member)
        .with_row_index()
        .with_columns(
            pl.when(pl.col("index") == 124)
            .then(pl.lit(1.0))
            .otherwise(pl.col("micro_pm25_mean"))
            .alias("micro_pm25_mean")
        )
        .drop("index")
    )
    rows.write_parquet(member)
    _rebind_agreement_panel_manifest(destination)

    with pytest.raises(RuntimeError, match="ineligible value is not null"):
        agreement.load_annual_agreement_panel(destination)


def test_rebinding_hashes_cannot_hide_reordered_physical_output_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    for name in ("paired_days", "exclusions"):
        member = destination / f"{name}.parquet"
        pl.read_parquet(member).reverse().write_parquet(member)
    _rebind_agreement_panel_manifest(destination)

    with pytest.raises(RuntimeError, match="physical output key order changed"):
        agreement.load_annual_agreement_panel(destination)


def test_the_writer_reloads_the_persisted_manifest_before_releasing_its_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    original_load = agreement._load_annual_agreement_panel_unlocked
    reloaded_while_locked = False

    def prove_lock(directory: Path) -> Any:
        nonlocal reloaded_while_locked
        with (
            pytest.raises(RuntimeError, match="checkpoint writer is active"),
            agreement._agreement_panel_lock(directory.parent / f".{directory.name}.lock"),
        ):
            pytest.fail("panel lock was released before manifest reload")
        reloaded_while_locked = True
        return original_load(directory)

    monkeypatch.setattr(agreement, "_load_annual_agreement_panel_unlocked", prove_lock)

    published = agreement.write_annual_agreement_panel(panel, destination=destination)

    assert reloaded_while_locked
    assert "generation_sha256" in published.manifest
    assert "generation_sha256" not in panel.manifest


@pytest.mark.parametrize("mutation", ["config", "summary", "calendar", "coverage"])
def test_rebound_panel_metadata_and_aggregates_must_match_the_prepared_rows_and_reviewed_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    if mutation == "config":
        manifest = deepcopy(panel.manifest)
        manifest["config"]["threads"] = 999
        changed = replace(panel, manifest=manifest)
    elif mutation == "summary":
        summary = deepcopy(panel.summary)
        summary["reason_counts"]["invented"] = 999
        manifest = {**panel.manifest, "summary_sha256": _canonical_hash(summary)}
        changed = replace(panel, summary=summary, manifest=manifest)
    elif mutation == "calendar":
        calendar = (
            panel.calendar.with_row_index()
            .with_columns(
                pl.when(pl.col("index") == 0)
                .then(pl.lit("e" * 64))
                .otherwise(pl.col("catalog_generation_sha256"))
                .alias("catalog_generation_sha256")
            )
            .drop("index")
        )
        changed = replace(panel, calendar=calendar)
    else:
        coverage = (
            panel.cohort_coverage.with_row_index()
            .with_columns(
                pl.when(pl.col("index") == 0)
                .then(pl.col("device_days") + 1)
                .otherwise(pl.col("device_days"))
                .alias("device_days")
            )
            .drop("index")
        )
        changed = replace(panel, cohort_coverage=coverage)

    with pytest.raises(RuntimeError, match="reviewed preparation changed"):
        agreement.write_annual_agreement_panel(
            changed,
            destination=tmp_path / f"published-{mutation}",
        )


def test_an_interrupted_panel_publish_leaves_no_partial_output_and_releases_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    original_replace = Path.replace

    def interrupt_publish(path: Path, target: Path) -> Path:
        if path.name.startswith(".published.staging-") and target == destination:
            raise KeyboardInterrupt("synthetic publish interruption")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupt_publish)
    with pytest.raises(KeyboardInterrupt, match="synthetic publish interruption"):
        agreement.write_annual_agreement_panel(panel, destination=destination)

    assert not destination.exists()
    assert not tuple(destination.parent.glob(".published.staging-*"))
    assert not tuple(destination.parent.glob(".published.backup-*"))
    monkeypatch.setattr(Path, "replace", original_replace)
    published = agreement.write_annual_agreement_panel(panel, destination=destination)
    assert published.manifest["complete"] is True


def test_a_successful_panel_move_followed_by_an_interrupt_rolls_back_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    original_replace = Path.replace

    def move_then_interrupt(path: Path, target: Path) -> Path:
        moved = original_replace(path, target)
        if path.name.startswith(".published.staging-") and target == destination:
            raise KeyboardInterrupt("synthetic post-move interruption")
        return moved

    monkeypatch.setattr(Path, "replace", move_then_interrupt)
    with pytest.raises(KeyboardInterrupt, match="synthetic post-move interruption"):
        agreement.write_annual_agreement_panel(panel, destination=destination)

    assert not destination.exists()
    assert not tuple(destination.parent.glob(".published.staging-*"))
    assert not tuple(destination.parent.glob(".published.backup-*"))
    monkeypatch.setattr(Path, "replace", original_replace)
    published = agreement.write_annual_agreement_panel(panel, destination=destination)
    assert published.manifest["complete"] is True


def test_a_final_persisted_reload_failure_rolls_back_the_published_generation_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    original_load = agreement._load_annual_agreement_panel_unlocked

    def fail_final_reload(_directory: Path) -> Any:
        raise RuntimeError("synthetic final persisted reload failure")

    monkeypatch.setattr(
        agreement,
        "_load_annual_agreement_panel_unlocked",
        fail_final_reload,
    )
    with pytest.raises(RuntimeError, match="synthetic final persisted reload failure"):
        agreement.write_annual_agreement_panel(panel, destination=destination)

    assert not destination.exists()
    assert not tuple(destination.parent.glob(".published.staging-*"))
    assert not tuple(destination.parent.glob(".published.backup-*"))
    monkeypatch.setattr(agreement, "_load_annual_agreement_panel_unlocked", original_load)
    published = agreement.write_annual_agreement_panel(panel, destination=destination)
    assert published.manifest["complete"] is True


def test_agreement_checkpoint_member_rejects_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    _annual_input, checkpoints, _config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    checkpoint_dir = checkpoints[0].directory
    checkpoint_file = checkpoint_dir / "paired_day.parquet"
    outside = tmp_path / "outside_checkpoint.parquet"
    try:
        os.link(checkpoint_file, outside)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hard link not supported in this environment")

    assert checkpoint_file.stat().st_nlink == 2

    with pytest.raises(RuntimeError, match="reviewed source is linked or outside"):
        agreement._assert_reviewed_direct_child(
            checkpoint_file,
            parent=checkpoint_dir,
            is_directory=False,
        )


def test_agreement_panel_inventory_rejects_hardlinked_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    annual_input, checkpoints, config = _annual_agreement_panel_fixture(
        tmp_path / "inputs", monkeypatch
    )
    monkeypatch.setattr(agreement, "load_annual_readiness_input", lambda _: annual_input)
    panel = agreement.prepare_annual_agreement_panel(annual_input, checkpoints, config)
    destination = tmp_path / "published"
    agreement.write_annual_agreement_panel(panel, destination=destination)
    calendar_path = destination / "calendar.parquet"
    outside = tmp_path / "outside_panel.parquet"
    try:
        os.link(calendar_path, outside)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hard link not supported in this environment")

    assert calendar_path.stat().st_nlink == 2

    with pytest.raises(RuntimeError, match="panel member is linked or outside generation"):
        agreement.load_annual_agreement_panel(destination)


def test_agreement_final_inventory_rejects_hardlinked_member(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    destination = tmp_path / "final_output"
    destination.mkdir(parents=True)
    for name in agreement._FINAL_MEMBER_NAMES:
        (destination / f"{name}.parquet").write_bytes(b"parquet-bytes")
    (destination / "summary.json").write_text("{}", encoding="utf-8")
    (destination / "manifest.json").write_text("{}", encoding="utf-8")

    folds_path = destination / "folds.parquet"
    outside = tmp_path / "outside_final.parquet"
    try:
        os.link(folds_path, outside)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hard link not supported in this environment")

    assert folds_path.stat().st_nlink == 2

    with pytest.raises(RuntimeError, match="panel member is linked or outside generation"):
        agreement._validate_final_inventory(destination, during_read=True)
