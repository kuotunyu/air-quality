from __future__ import annotations

import ast
import copy
import json
import math
import re
import subprocess
import sys
from dataclasses import replace
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import twair.analysis.micro_sensor_agreement_audit as audit
from twair.analysis.micro_sensor_agreement_audit import (
    AgreementAuditConfig,
    FrozenAuditInputs,
    load_frozen_agreement_audit_inputs,
    load_micro_sensor_agreement_audit_config,
)
from twair.analysis.micro_sensor_annual_readiness import (
    ANNUAL_CALENDAR_SCHEMA,
    ANNUAL_COHORT_THRESHOLD_SCHEMA,
    ANNUAL_DEVICE_COHORT_SCHEMA,
    ANNUAL_DEVICE_DAY_SCHEMA,
    ANNUAL_EXCLUSION_SCHEMA,
)
from twair.config import ConfigError, load_conf
from twair.net import sha256_file

_ANNUAL_SCHEMAS = {
    "calendar_coverage": ANNUAL_CALENDAR_SCHEMA,
    "device_days": ANNUAL_DEVICE_DAY_SCHEMA,
    "device_cohorts": ANNUAL_DEVICE_COHORT_SCHEMA,
    "cohort_thresholds": ANNUAL_COHORT_THRESHOLD_SCHEMA,
    "exclusions": ANNUAL_EXCLUSION_SCHEMA,
}
AGREEMENT_CALENDAR_SCHEMA = (
    ("date", pl.Date),
    ("calendar_state", pl.String),
    ("catalog_generation_sha256", pl.String),
    ("parsed_generation_sha256", pl.String),
)
AGREEMENT_PAIRED_DAY_SCHEMA = (
    ("radius_km", pl.Float64),
    ("calendar_state", pl.String),
    ("quarter", pl.Int64),
    ("date", pl.Date),
    ("device_id", pl.String),
    ("station_name", pl.String),
    ("distance_km", pl.Float64),
    ("lon_min", pl.Float64),
    ("lat_min", pl.Float64),
    ("pm25_source_rows", pl.Int64),
    ("pm25_observed_hours", pl.Int64),
    ("humidity_source_rows", pl.Int64),
    ("humidity_observed_hours", pl.Int64),
    ("temperature_source_rows", pl.Int64),
    ("temperature_observed_hours", pl.Int64),
    ("trio_observed_hours", pl.Int64),
    ("micro_pm25_mean", pl.Float64),
    ("micro_humidity_mean", pl.Float64),
    ("micro_temperature_mean", pl.Float64),
    ("ground_pm25_mean", pl.Float64),
    ("reason", pl.String),
)
AGREEMENT_EXCLUSION_SCHEMA = (
    ("radius_km", pl.Float64),
    ("date", pl.Date),
    ("device_id", pl.String),
    ("station_name", pl.String),
    ("quarter", pl.Int64),
    ("reason", pl.String),
)
AGREEMENT_PREDICTION_SCHEMA = (
    ("evaluation", pl.String),
    ("fold", pl.String),
    ("fold_state", pl.String),
    ("radius_km", pl.Float64),
    ("date", pl.Date),
    ("device_id", pl.String),
    ("station_name", pl.String),
    ("station_fold", pl.Int64),
    ("quarter", pl.Int64),
    ("train_membership_sha256", pl.String),
    ("test_membership_sha256", pl.String),
    ("test_truth_sha256", pl.String),
    ("model", pl.String),
    ("model_features", pl.String),
    ("y_true", pl.Float64),
    ("y_pred", pl.Float64),
)
AGREEMENT_SCORE_SCHEMA = (
    ("scope", pl.String),
    ("evaluation", pl.String),
    ("fold", pl.String),
    ("radius_km", pl.Float64),
    ("model", pl.String),
    ("unit", pl.String),
    ("state", pl.String),
    ("n", pl.Int64),
    ("intended_n", pl.Int64),
    ("membership_sha256", pl.String),
    ("truth_sha256", pl.String),
    ("scored_membership_sha256", pl.String),
    ("scored_truth_sha256", pl.String),
    ("total_folds", pl.Int64),
    ("scored_folds", pl.Int64),
    ("unscored_folds_sha256", pl.String),
    ("rmse", pl.Float64),
    ("mae", pl.Float64),
    ("r2", pl.Float64),
    ("bias", pl.Float64),
    ("absolute_bias", pl.Float64),
)
AGREEMENT_DELTA_SCHEMA = (
    *AGREEMENT_SCORE_SCHEMA[:4],
    ("unit", pl.String),
    ("model", pl.String),
    ("baseline_model", pl.String),
    *AGREEMENT_SCORE_SCHEMA[6:16],
    ("delta_rmse", pl.Float64),
    ("delta_mae", pl.Float64),
    ("delta_r2", pl.Float64),
    ("delta_bias", pl.Float64),
    ("delta_absolute_bias", pl.Float64),
    ("improved_rmse", pl.Boolean),
    ("improved_mae", pl.Boolean),
    ("improved_r2", pl.Boolean),
    ("improved_absolute_bias", pl.Boolean),
)
_FOLD_MEMBERSHIP_SCHEMA = (
    ("evaluation", pl.String),
    ("fold", pl.String),
    ("role", pl.String),
    ("station_fold", pl.Int64),
    *AGREEMENT_PAIRED_DAY_SCHEMA,
    ("fold_state", pl.String),
    ("fold_reason", pl.String),
    ("train_rows", pl.Int64),
    ("test_rows", pl.Int64),
    ("train_unique_targets", pl.Int64),
    ("test_unique_targets", pl.Int64),
    ("train_membership_sha256", pl.String),
    ("test_membership_sha256", pl.String),
    ("test_truth_sha256", pl.String),
)
_FOLD_SCHEMA = (
    ("evaluation", pl.String),
    ("fold", pl.String),
    ("fold_state", pl.String),
    ("fold_reason", pl.String),
    ("train_rows", pl.Int64),
    ("test_rows", pl.Int64),
    ("train_unique_targets", pl.Int64),
    ("test_unique_targets", pl.Int64),
    ("train_membership_sha256", pl.String),
    ("test_membership_sha256", pl.String),
    ("test_truth_sha256", pl.String),
    ("train_devices", pl.Int64),
    ("test_devices", pl.Int64),
    ("train_stations", pl.Int64),
    ("test_stations", pl.Int64),
    ("train_dates", pl.Int64),
    ("test_dates", pl.Int64),
    ("device_overlap", pl.Int64),
    ("excluded_rows", pl.Int64),
)
_SATELLITE_SCHEMA = (
    ("source", pl.String),
    ("station_name", pl.String),
    ("month", pl.Date),
    ("satellite_value", pl.Float64),
    ("satellite_unit", pl.String),
    ("ground_value", pl.Float64),
    ("ground_unit", pl.String),
    ("satellite_observed", pl.Boolean),
    ("ground_row_present", pl.Boolean),
    ("ground_meets_threshold", pl.Boolean),
    ("ground_observed", pl.Boolean),
    ("ground_withheld", pl.Boolean),
    ("pair_observed", pl.Boolean),
    ("collection_id", pl.String),
    ("band", pl.String),
    ("sample_scale_m", pl.Int32),
)
_AGREEMENT_SCHEMAS = {
    "calendar": AGREEMENT_CALENDAR_SCHEMA,
    "paired_days": AGREEMENT_PAIRED_DAY_SCHEMA,
    "exclusions": AGREEMENT_EXCLUSION_SCHEMA,
    "fold_membership": _FOLD_MEMBERSHIP_SCHEMA,
    "folds": _FOLD_SCHEMA,
    "predictions": AGREEMENT_PREDICTION_SCHEMA,
    "scores": AGREEMENT_SCORE_SCHEMA,
    "deltas": AGREEMENT_DELTA_SCHEMA,
}
_COORDINATE_FIELDS = (
    "station_name",
    "lon",
    "lat",
    "geo_source",
    "geo_source_record_namespace",
    "geo_source_record_id",
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
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _schema_dict(schema: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...]) -> dict[str, Any]:
    return dict(schema)


def _empty(schema: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...]) -> pl.DataFrame:
    return pl.DataFrame(schema=_schema_dict(schema))


def _default(dtype: pl.DataType | type[pl.DataType]) -> object:
    if dtype == pl.String:
        return ""
    if dtype in (pl.Float32, pl.Float64):
        return 0.0
    if dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64):
        return 0
    if dtype == pl.Boolean:
        return False
    if dtype == pl.Date:
        return date(2025, 10, 1)
    raise AssertionError(f"unsupported fixture dtype: {dtype}")


def _frame(
    schema: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...],
    rows: list[dict[str, object]],
) -> pl.DataFrame:
    complete = [
        {name: row.get(name, _default(dtype)) for name, dtype in schema}
        for row in rows
    ]
    return pl.DataFrame(complete, schema=_schema_dict(schema))


def _fixture_digest(frame: pl.DataFrame, *, truth: bool = False) -> str:
    identity = (
        "radius_km",
        "date",
        "device_id",
        "station_name",
        "station_fold",
        "quarter",
    )
    columns = (*identity, "ground_pm25_mean") if truth else identity
    records = (
        frame.select(*columns)
        .sort(*identity)
        .with_columns(pl.col("date").cast(pl.String))
        .to_dicts()
    )
    return _canonical_hash(records)


def _source_fold_artifacts(
    paired: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    geography = _geography()
    inventory = (
        paired.select("station_name")
        .unique()
        .join(geography.select("station_name", "airzone_official"), on="station_name")
    )
    assigned: list[dict[str, object]] = []
    position = 0
    strata = sorted(
        inventory["airzone_official"].unique().to_list(),
        key=lambda value: (value is None, "" if value is None else str(value)),
    )
    for stratum in strata:
        stations = inventory.filter(
            pl.col("airzone_official").is_null()
            if stratum is None
            else pl.col("airzone_official") == stratum
        ).sort("station_name")
        for station in stations["station_name"]:
            assigned.append(
                {
                    "station_name": station,
                    "airzone_official": stratum,
                    "station_fold": position % 5,
                }
            )
            position += 1
    eligible = paired.filter(pl.col("reason") == "eligible").join(
        pl.DataFrame(assigned).select("station_name", "station_fold"),
        on="station_name",
    )
    split_frames: list[pl.DataFrame] = []
    for station_fold in range(5):
        split_frames.append(
            eligible.with_columns(
                pl.lit("held_station").alias("evaluation"),
                pl.lit(f"held_station_{station_fold:02d}").alias("fold"),
                pl.when(pl.col("station_fold") == station_fold)
                .then(pl.lit("test"))
                .otherwise(pl.lit("train"))
                .alias("role"),
            )
        )
    for quarter in range(1, 5):
        split_frames.append(
            eligible.with_columns(
                pl.lit("held_quarter").alias("evaluation"),
                pl.lit(f"held_quarter_{quarter:02d}").alias("fold"),
                pl.when(pl.col("quarter") == quarter)
                .then(pl.lit("test"))
                .otherwise(pl.lit("train"))
                .alias("role"),
            )
        )
    for station_fold in range(5):
        for quarter in range(1, 5):
            split_frames.append(
                eligible.with_columns(
                    pl.lit("joint").alias("evaluation"),
                    pl.lit(f"joint_{station_fold:02d}_{quarter:02d}").alias("fold"),
                    pl.when(
                        (pl.col("station_fold") == station_fold)
                        & (pl.col("quarter") == quarter)
                    )
                    .then(pl.lit("test"))
                    .when(
                        (pl.col("station_fold") != station_fold)
                        & (pl.col("quarter") != quarter)
                    )
                    .then(pl.lit("train"))
                    .otherwise(pl.lit("excluded"))
                    .alias("role"),
                )
            )
    memberships = pl.concat(split_frames).select(
        "evaluation", "fold", "role", "station_fold", *paired.columns
    )
    bound: list[pl.DataFrame] = []
    fold_rows: list[dict[str, object]] = []
    for split in memberships.partition_by("evaluation", "fold"):
        train = split.filter(pl.col("role") == "train")
        test = split.filter(pl.col("role") == "test")
        train_targets = train["ground_pm25_mean"].n_unique()
        test_targets = test["ground_pm25_mean"].n_unique()
        if train.is_empty():
            state = "unscored_empty_train"
        elif train.height < 2 or train_targets < 2:
            state = "unscored_insufficient_train"
        elif test.is_empty():
            state = "unscored_empty_test"
        elif test_targets < 2:
            state = "unscored_single_target"
        else:
            state = "scored"
        train_hash = _fixture_digest(train)
        test_hash = _fixture_digest(test)
        truth_hash = _fixture_digest(test, truth=True)
        enriched = split.with_columns(
            pl.lit(state).alias("fold_state"),
            pl.lit(state).alias("fold_reason"),
            pl.lit(train.height, dtype=pl.Int64).alias("train_rows"),
            pl.lit(test.height, dtype=pl.Int64).alias("test_rows"),
            pl.lit(train_targets, dtype=pl.Int64).alias("train_unique_targets"),
            pl.lit(test_targets, dtype=pl.Int64).alias("test_unique_targets"),
            pl.lit(train_hash).alias("train_membership_sha256"),
            pl.lit(test_hash).alias("test_membership_sha256"),
            pl.lit(truth_hash).alias("test_truth_sha256"),
        ).select(*dict(_FOLD_MEMBERSHIP_SCHEMA))
        bound.append(enriched)
        train_devices = set(train["device_id"])
        test_devices = set(test["device_id"])
        fold_rows.append(
            {
                "evaluation": split["evaluation"][0],
                "fold": split["fold"][0],
                "fold_state": state,
                "fold_reason": state,
                "train_rows": train.height,
                "test_rows": test.height,
                "train_unique_targets": train_targets,
                "test_unique_targets": test_targets,
                "train_membership_sha256": train_hash,
                "test_membership_sha256": test_hash,
                "test_truth_sha256": truth_hash,
                "train_devices": len(train_devices),
                "test_devices": len(test_devices),
                "train_stations": train["station_name"].n_unique(),
                "test_stations": test["station_name"].n_unique(),
                "train_dates": train["date"].n_unique(),
                "test_dates": test["date"].n_unique(),
                "device_overlap": len(train_devices & test_devices),
                "excluded_rows": split.filter(pl.col("role") == "excluded").height,
            }
        )
    membership = pl.concat(bound).sort(
        "evaluation", "fold", "role", "radius_km", "date", "device_id"
    )
    folds = _frame(_FOLD_SCHEMA, fold_rows).sort("evaluation", "fold")
    prediction_rows: list[pl.DataFrame] = []
    features = {
        "pooled_micro_ridge": ("micro_pm25_mean",),
        "pooled_weather_ridge": (
            "micro_pm25_mean",
            "micro_humidity_mean",
            "micro_temperature_mean",
        ),
    }
    for fold in folds.filter(pl.col("fold_state") == "scored")["fold"]:
        split = membership.filter(pl.col("fold") == fold)
        train = split.filter(pl.col("role") == "train")
        test = split.filter(pl.col("role") == "test")
        base = test.select(
            "evaluation",
            "fold",
            "fold_state",
            "radius_km",
            "date",
            "device_id",
            "station_name",
            "station_fold",
            "quarter",
            "train_membership_sha256",
            "test_membership_sha256",
            "test_truth_sha256",
        )
        prediction_rows.append(
            base.with_columns(
                pl.lit("raw_micro").alias("model"),
                pl.lit("micro_pm25_mean").alias("model_features"),
                test["ground_pm25_mean"].alias("y_true"),
                test["micro_pm25_mean"].alias("y_pred"),
            )
        )
        counts = train.group_by("station_name", "date").len()
        weights = (
            train.select("station_name", "date")
            .join(counts, on=("station_name", "date"), how="left")["len"]
            .cast(pl.Float64)
            .pow(-1)
            .to_numpy()
        )
        for model, model_features in features.items():
            pipeline = Pipeline(
                [("scale", StandardScaler()), ("ridge", Ridge(alpha=1.0))]
            )
            pipeline.fit(
                train.select(*model_features).to_numpy(),
                train["ground_pm25_mean"].to_numpy(),
                scale__sample_weight=weights,
                ridge__sample_weight=weights,
            )
            prediction_rows.append(
                base.with_columns(
                    pl.lit(model).alias("model"),
                    pl.lit(",".join(model_features)).alias("model_features"),
                    test["ground_pm25_mean"].alias("y_true"),
                    pl.Series(
                        "y_pred",
                        pipeline.predict(test.select(*model_features).to_numpy()),
                    ),
                )
            )
    predictions = pl.concat(prediction_rows).select(*dict(AGREEMENT_PREDICTION_SCHEMA)).sort(
        "evaluation", "fold", "model", "date", "device_id"
    )
    return membership, folds, predictions


def _write_members(
    directory: Path,
    frames: dict[str, pl.DataFrame],
) -> tuple[dict[str, dict[str, object]], dict[str, int], dict[str, dict[str, str]]]:
    members: dict[str, dict[str, object]] = {}
    rows: dict[str, int] = {}
    schemas: dict[str, dict[str, str]] = {}
    for name, frame in frames.items():
        path = directory / f"{name}.parquet"
        frame.write_parquet(path)
        members[name] = {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        rows[name] = frame.height
        schemas[name] = {column: str(dtype) for column, dtype in frame.schema.items()}
    return members, rows, schemas


def _geography() -> pl.DataFrame:
    zone_sizes = (5, 4, 4)
    rows: list[dict[str, object]] = []
    index = 0
    for zone_index, size in enumerate(zone_sizes):
        for _ in range(size):
            rows.append(
                {
                    "station_name": f"station-{index:02d}",
                    "lon": 120.0 + index / 100,
                    "lat": 23.0 + index / 100,
                    "geo_source": "fixture",
                    "geo_source_record_namespace": "fixture-stations",
                    "geo_source_record_id": f"fixture-{index:02d}",
                    "airzone_official": f"zone-{zone_index + 1}",
                }
            )
            index += 1
    rows.append(
        {
            "station_name": "historical-unzoned",
            "lon": 121.75,
            "lat": 25.15,
            "geo_source": "fixture-historical",
            "geo_source_record_namespace": "fixture-stations",
            "geo_source_record_id": "fixture-historical",
            "airzone_official": None,
        }
    )
    return pl.DataFrame(rows)


def _agreement_frames() -> dict[str, pl.DataFrame]:
    eligible_rows: list[dict[str, object]] = []
    for station_index in range(12):
        for day in range(1, 4):
            observed_day = 1 if station_index == 0 and day == 2 else day
            device_suffix = "00b" if station_index == 0 and day == 2 else f"{station_index:02d}"
            eligible_rows.append(
                {
                    "radius_km": 0.5,
                    "calendar_state": "complete",
                    "quarter": 4,
                    "date": date(2025, 10, observed_day),
                    "device_id": f"device-{device_suffix}",
                    "station_name": f"station-{station_index:02d}",
                    "distance_km": 0.1,
                    "micro_pm25_mean": float(10 + station_index + observed_day),
                    "micro_humidity_mean": float(60 + observed_day),
                    "micro_temperature_mean": float(24 + observed_day),
                    "ground_pm25_mean": float(11 + station_index + observed_day),
                    "reason": "eligible",
                }
            )
    paired_rows = [
        *eligible_rows,
        {
            "radius_km": 0.5,
            "calendar_state": "complete",
            "quarter": 4,
            "date": date(2025, 10, 1),
            "device_id": "device-12",
            "station_name": "station-12",
            "distance_km": 0.1,
            "reason": "insufficient_micro_hours",
        },
    ]
    paired = _frame(AGREEMENT_PAIRED_DAY_SCHEMA, paired_rows)
    memberships, folds, predictions = _source_fold_artifacts(paired)
    return {
        "calendar": _frame(
            AGREEMENT_CALENDAR_SCHEMA,
            [
                {
                    "date": date(2025, 10, day),
                    "calendar_state": "complete",
                    "catalog_generation_sha256": "a" * 64,
                    "parsed_generation_sha256": "b" * 64,
                }
                for day in range(1, 4)
            ],
        ),
        "paired_days": paired,
        "exclusions": _frame(
            AGREEMENT_EXCLUSION_SCHEMA,
            [
                {
                    "radius_km": 0.5,
                    "date": date(2025, 10, 1),
                    "device_id": "device-12",
                    "station_name": "station-12",
                    "quarter": 4,
                    "reason": "insufficient_micro_hours",
                }
            ],
        ),
        "fold_membership": memberships,
        "folds": folds,
        "predictions": predictions,
        "scores": _empty(AGREEMENT_SCORE_SCHEMA),
        "deltas": _empty(AGREEMENT_DELTA_SCHEMA),
    }


def _create_frozen_source(root: Path) -> tuple[dict[str, int], dict[str, int]]:
    reviewed_geography = _geography()
    reviewed_geography_sha256 = _canonical_hash(
        reviewed_geography.select(*_COORDINATE_FIELDS).sort("station_name").to_dicts()
    )
    annual_staging = root / "annual-staging"
    annual_staging.mkdir(parents=True)
    annual_frames = {name: _empty(schema) for name, schema in _ANNUAL_SCHEMAS.items()}
    annual_members, annual_rows, _ = _write_members(annual_staging, annual_frames)
    annual_summary = {"fixture": "annual", "output_rows": annual_rows}
    annual_summary_path = annual_staging / "summary.json"
    _write_json(annual_summary_path, annual_summary)
    annual_identity = {
        "schema_version": 1,
        "analysis": "annual_micro_sensor_readiness",
        "config": {"fixture": True},
        "inputs": {"reviewed_geography_sha256": reviewed_geography_sha256},
        "checkpoint_inventory": [],
        "claim_boundary": {},
        "output_rows": annual_rows,
        "members": annual_members,
        "summary_file": {
            "path": "summary.json",
            "bytes": annual_summary_path.stat().st_size,
            "sha256": sha256_file(annual_summary_path),
        },
    }
    annual_generation = _canonical_hash(annual_identity)
    annual_manifest = {
        **annual_identity,
        "complete": True,
        "generated_at": "2026-08-29T00:00:00Z",
        "generation_sha256": annual_generation,
        "git_sha": "e4839bc",
        "git_dirty": False,
        "checkpoint_run": "fixture",
    }
    _write_json(annual_staging / "manifest.json", annual_manifest)
    annual_dir = (
        root
        / "outputs"
        / "micro_sensor_annual_readiness"
        / "generations"
        / annual_generation
    )
    annual_dir.parent.mkdir(parents=True)
    annual_staging.rename(annual_dir)

    agreement_staging = root / "agreement-staging"
    agreement_staging.mkdir()
    agreement_frames = _agreement_frames()
    agreement_members, agreement_rows, agreement_schemas = _write_members(
        agreement_staging, agreement_frames
    )
    agreement_summary = {"fixture": "agreement", "output_rows": agreement_rows}
    agreement_summary_path = agreement_staging / "summary.json"
    _write_json(agreement_summary_path, agreement_summary)
    agreement_identity = {
        "schema_version": 1,
        "analysis": "q4_supported_cross_station_agreement",
        "annual_generation_sha256": annual_generation,
        "panel_generation_sha256": "c" * 64,
        "evaluation_generation_sha256": "d" * 64,
        "panel_manifest": {"fixture": True},
        "evaluation_manifest": {"fixture": True},
        "checkpoint_inventory": [],
        "config": {"fixture": True},
        "claim_boundary": {},
        "output_rows": agreement_rows,
        "schemas": agreement_schemas,
        "members": agreement_members,
        "summary_file": {
            "path": "summary.json",
            "bytes": agreement_summary_path.stat().st_size,
            "sha256": sha256_file(agreement_summary_path),
        },
        "summary_sha256": _canonical_hash(agreement_summary),
        "git_sha": "b7bff3e",
        "git_dirty": False,
    }
    agreement_generation = _canonical_hash(agreement_identity)
    agreement_manifest = {
        **agreement_identity,
        "complete": True,
        "generated_at": "2026-08-29T00:00:00Z",
        "generation_sha256": agreement_generation,
    }
    _write_json(agreement_staging / "manifest.json", agreement_manifest)
    agreement_dir = (
        root
        / "outputs"
        / "micro_sensor_annual_agreement"
        / "generations"
        / agreement_generation
    )
    agreement_dir.parent.mkdir(parents=True)
    agreement_staging.rename(agreement_dir)

    satellite_dir = (
        root
        / "outputs"
        / "m8_satellite"
        / "generations"
        / ("e" * 64)
        / "year=2025"
    )
    satellite_dir.mkdir(parents=True)
    satellite_rows: list[dict[str, object]] = [
        {
            "source": source,
            "station_name": f"station-{station_index:02d}",
            "month": date(2025, 10, 1),
            "satellite_value": float(station_index + 1),
            "satellite_unit": "fixture",
            "ground_value": float(station_index + 2),
            "ground_unit": "ug/m3",
            "satellite_observed": True,
            "ground_row_present": True,
            "ground_meets_threshold": True,
            "ground_observed": True,
            "ground_withheld": False,
            "pair_observed": True,
            "collection_id": f"fixture-{source}",
            "band": "fixture",
            "sample_scale_m": 1000,
        }
        for station_index in range(12)
        for source in ("maiac_aod", "s5p_no2", "s5p_so2")
    ]
    satellite_path = satellite_dir / "panel.parquet"
    _frame(_SATELLITE_SCHEMA, satellite_rows).write_parquet(satellite_path)
    coordinate_records = (
        reviewed_geography.select(*_COORDINATE_FIELDS).sort("station_name").to_dicts()
    )
    airzone_records = (
        reviewed_geography.select(*_COORDINATE_FIELDS, "airzone_official")
        .sort("station_name")
        .to_dicts()
    )
    _write_json(
        root / "fixture-bindings.json",
        {
            "annual_generation_sha256": annual_generation,
            "annual_manifest_sha256": sha256_file(annual_dir / "manifest.json"),
            "agreement_generation_sha256": agreement_generation,
            "agreement_manifest_sha256": sha256_file(agreement_dir / "manifest.json"),
            "agreement_summary_sha256": _canonical_hash(agreement_summary),
            "satellite_generation_sha256": "e" * 64,
            "satellite_panel_bytes": satellite_path.stat().st_size,
            "satellite_panel_sha256": sha256_file(satellite_path),
            "reviewed_geography_sha256": _canonical_hash(coordinate_records),
            "reviewed_airzone_sha256": _canonical_hash(airzone_records),
        },
    )
    return annual_rows, agreement_rows


def shipped_config_payload() -> dict[str, Any]:
    return copy.deepcopy(load_conf("micro_sensor_agreement_audit"))


def mutate_config_path(payload: dict[str, Any], path: str) -> None:
    _, field = path.split(".", maxsplit=1)
    analysis = payload["analysis"]
    if field == "claim_boundary":
        analysis[field]["validated_calibration"] = True
    elif field.endswith("sha256"):
        analysis[field] = "0" * 64
    elif field == "ridge_alpha":
        analysis[field] = 2.0
    else:
        analysis[field] = int(analysis[field]) + 1


def synthetic_audit_config(root: Path) -> AgreementAuditConfig:
    bindings = json.loads((root / "fixture-bindings.json").read_text(encoding="utf-8"))
    return replace(
        load_micro_sensor_agreement_audit_config(),
        **bindings,
    )


def mutate_frozen_fixture(root: Path, mutation: str) -> None:
    annual_dir = next(
        (root / "outputs" / "micro_sensor_annual_readiness" / "generations").iterdir()
    )
    agreement_dir = next(
        (root / "outputs" / "micro_sensor_annual_agreement" / "generations").iterdir()
    )
    if mutation == "manifest_hash":
        with (annual_dir / "manifest.json").open("ab") as handle:
            handle.write(b"\n")
    elif mutation == "member_hash":
        with (annual_dir / "calendar_coverage.parquet").open("ab") as handle:
            handle.write(b"changed")
    elif mutation == "extra_member":
        (agreement_dir / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    else:
        manifest = agreement_dir / "manifest.json"
        target = root / "linked-manifest.json"
        target.write_bytes(manifest.read_bytes())
        manifest.unlink()
        try:
            manifest.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"Windows denied symlink creation: {exc}")


@pytest.fixture
def audit_source_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    annual_rows, agreement_rows = _create_frozen_source(tmp_path)
    monkeypatch.setattr(audit, "_ANNUAL_EXPECTED_ROWS", annual_rows)
    monkeypatch.setattr(audit, "_AGREEMENT_EXPECTED_ROWS", agreement_rows)
    monkeypatch.setattr(audit, "resolve_station_geo", _geography)
    return tmp_path


@pytest.fixture
def loaded_audit_fixture(
    audit_source_fixture: Path,
) -> tuple[FrozenAuditInputs, AgreementAuditConfig]:
    config = synthetic_audit_config(audit_source_fixture)
    return load_frozen_agreement_audit_inputs(audit_source_fixture, config), config


@pytest.fixture
def scored_prediction_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    source = _agreement_frames()
    folds = source["folds"].select(
        "evaluation",
        "fold",
        pl.when(pl.col("fold") == "held_station_00")
        .then(pl.lit("scored"))
        .otherwise(pl.lit("unscored_empty_test"))
        .alias("state"),
        pl.when(pl.col("fold") == "held_station_00")
        .then(pl.col("test_rows"))
        .otherwise(pl.lit(0, dtype=pl.Int64))
        .alias("test_rows"),
        "test_membership_sha256",
        "test_truth_sha256",
    )
    predictions = source["predictions"].filter(pl.col("fold") == "held_station_00")
    station_days = predictions.select("station_name", "date").unique().sort(
        "station_name", "date"
    )
    first = station_days.row(0, named=True)
    second = station_days.row(1, named=True)

    def duplicate(station_day: dict[str, object], suffix: str) -> pl.DataFrame:
        return predictions.filter(
            (pl.col("station_name") == station_day["station_name"])
            & (pl.col("date") == station_day["date"])
        ).with_columns((pl.col("device_id") + pl.lit(suffix)).alias("device_id"))

    predictions = pl.concat(
        [
            predictions,
            duplicate(first, "-extra"),
            duplicate(second, "-extra-1"),
            duplicate(second, "-extra-2"),
        ]
    )
    return predictions, folds


def audit_config() -> AgreementAuditConfig:
    return load_micro_sensor_agreement_audit_config()


def bootstrap_pairs() -> tuple[tuple[str, str], ...]:
    return (("candidate", "comparator"),)


def clustered_prediction_fixture() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for station_index, station in enumerate(("alpha", "beta", "gamma")):
        for day in (1, 2):
            truth = float(10 + station_index + day)
            for model, error in (("comparator", 1.0), ("candidate", station_index / 2)):
                rows.append(
                    {
                        "station_name": station,
                        "date": date(2025, 10, day),
                        "device_id": f"{station}-{day}",
                        "model": model,
                        "y_true": truth,
                        "y_pred": truth + error,
                    }
                )
    return pl.DataFrame(rows)


def duplicated_device_fixture() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for device in ("alpha-1", "alpha-2", "alpha-3", "alpha-4"):
        for model in ("comparator", "candidate"):
            rows.append(
                {
                    "station_name": "alpha",
                    "date": date(2025, 10, 1),
                    "device_id": device,
                    "model": model,
                    "y_true": 0.0,
                    "y_pred": 0.0,
                }
            )
    for model, prediction in (("comparator", 0.0), ("candidate", 2.0)):
        rows.append(
            {
                "station_name": "beta",
                "date": date(2025, 10, 1),
                "device_id": "beta-1",
                "model": model,
                "y_true": 0.0,
                "y_pred": prediction,
            }
        )
    return pl.DataFrame(rows)


def expected_station_equal_delta() -> float:
    return math.sqrt(2.0)


def one_station_fixture() -> pl.DataFrame:
    return clustered_prediction_fixture().filter(pl.col("station_name") == "alpha")


def permutation_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    geography = pl.DataFrame(
        {
            "station_name": ["alpha", "beta", "gamma", "delta"],
            "airzone_official": ["north", "north", "south", "south"],
        }
    )
    station_days = pl.DataFrame(
        [
            {
                "station_name": station,
                "date": date(2025, 10, day),
                "ground_pm25_mean": float(index * 10 + day),
            }
            for day in (1, 2)
            for index, station in enumerate(("alpha", "beta", "gamma", "delta"), start=1)
        ]
    )
    return station_days, geography


def control_fixture() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    source = _agreement_frames()
    folds = source["folds"].rename(
        {"fold_state": "state", "fold_reason": "reason"}
    ).with_columns(
        pl.when(pl.col("fold") == "held_station_00")
        .then(pl.lit("scored"))
        .otherwise(pl.lit("unscored_empty_test"))
        .alias("state")
    )
    return source["fold_membership"], folds, _geography(), satellite_panel_fixture()


def singleton_zone_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    station_days = pl.DataFrame(
        {
            "station_name": ["singleton"],
            "date": [date(2025, 10, 1)],
            "ground_pm25_mean": [17.0],
        }
    )
    geography = pl.DataFrame(
        {"station_name": ["singleton"], "airzone_official": ["island"]}
    )
    return station_days, geography


def target_shift_fixture() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "station_name": station,
                "date": date(2025, 10, 1) + timedelta(days=offset),
                "ground_pm25_mean": float(offset + station_index),
                "fold": f"held_station_{station_index:02d}",
            }
            for station_index, station in enumerate(("alpha", "beta"))
            for offset in range(61)
        ]
    )


def eligible_station_months_fixture() -> pl.DataFrame:
    geography = _geography().select("station_name", "airzone_official")
    return pl.DataFrame(
        {
            "station_name": [f"station-{index:02d}" for index in range(12)],
            "month": [date(2025, 10, 1)] * 12,
        }
    ).join(geography, on="station_name")


def satellite_panel_fixture() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for station_index in range(12):
        for source_index, source in enumerate(("maiac_aod", "s5p_no2", "s5p_so2")):
            rows.append(
                {
                    "station_name": f"station-{station_index:02d}",
                    "month": date(2025, 10, 1),
                    "source": source,
                    "satellite_value": float(100 * station_index + source_index),
                    "pair_observed": True,
                }
            )
    return pl.DataFrame(rows)


def distinguishable_satellite_context_fixture() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "station_name": station,
                "month": date(2025, 10, 1),
                "airzone_official": "shared-zone",
                "maiac_aod": float(index * 100 + 1),
                "s5p_no2": float(index * 100 + 2),
                "s5p_so2": float(index * 100 + 3),
            }
            for index, station in enumerate(("alpha", "beta", "gamma"), start=1)
        ]
    )


def membership_hash(frame: pl.DataFrame) -> str:
    return _fixture_digest(frame)


def neighbor_fixture(buffer_km: float) -> tuple[pl.DataFrame, pl.DataFrame]:
    station_lon = 121.0
    station_lat = 25.0
    angular_buffer_degrees = math.degrees(buffer_km / 6371.0088)
    coordinates = {
        "inside": (station_lon, station_lat + 0.99 * angular_buffer_degrees),
        "outside": (station_lon, station_lat + 1.01 * angular_buffer_degrees),
        "held-test": (station_lon, station_lat),
    }
    rows: list[dict[str, object]] = []
    for day in (1, 2):
        for device_id, role in (
            ("inside", "train"),
            ("outside", "train"),
            ("held-test", "test"),
        ):
            lon, lat = coordinates[device_id]
            rows.append(
                {
                    "evaluation": "held_station",
                    "fold": "held_station_00",
                    "role": role,
                    "station_fold": 0 if role == "test" else 1,
                    "radius_km": 0.5,
                    "calendar_state": "complete",
                    "quarter": 4,
                    "date": date(2025, 10, day),
                    "device_id": device_id,
                    "station_name": "held-station" if role == "test" else "train-station",
                    "distance_km": 0.1,
                    "lon_min": lon,
                    "lat_min": lat,
                    "micro_pm25_mean": float(10 + day),
                    "micro_humidity_mean": float(60 + day),
                    "micro_temperature_mean": float(24 + day),
                    "ground_pm25_mean": float(11 + day),
                    "reason": "eligible",
                    "fold_state": "scored",
                    "fold_reason": "scored",
                }
            )
    membership = _frame(_FOLD_MEMBERSHIP_SCHEMA, rows).sort("role", "date", "device_id")
    geography = pl.DataFrame(
        {
            "station_name": ["held-station"],
            "lon": [station_lon],
            "lat": [station_lat],
        }
    )
    return membership, geography


def all_training_inside_fixture() -> tuple[pl.DataFrame, pl.DataFrame]:
    membership, geography = neighbor_fixture(2.0)
    return membership.filter(pl.col("device_id") != "outside"), geography


def fusion_gate_fixture() -> tuple[pl.DataFrame, ...]:
    fold_audit = pl.DataFrame(
        {
            "evaluation": ["held_station"],
            "fold": ["held_station_00"],
            "state": ["scored"],
        }
    )
    scores = pl.DataFrame(
        [
            {
                "scope": "overall",
                "evaluation": "held_station",
                "fold": None,
                "model": model,
                "unit": "station_day",
                "metric": "rmse",
                "state": "scored",
                "value": value,
            }
            for model, value in (
                ("raw_micro", 4.1894040401),
                ("pooled_micro_ridge", 4.6688480256),
                ("pooled_weather_ridge", 4.7206675171),
            )
        ],
        schema_overrides={"fold": pl.String, "value": pl.Float64},
    )
    deltas = pl.DataFrame(
        [
            {
                "scope": "overall",
                "evaluation": "held_station",
                "fold": None,
                "model": model,
                "baseline_model": "raw_micro",
                "unit": unit,
                "metric": "rmse",
                "state": "scored",
                "value": value,
            }
            for unit, values in (
                ("station_day", (0.4794439855, 0.5312634770)),
                ("device_day", (-1.3275839999, -1.2511376286)),
            )
            for model, value in zip(
                ("pooled_micro_ridge", "pooled_weather_ridge"), values, strict=True
            )
        ],
        schema_overrides={"fold": pl.String, "value": pl.Float64},
    )
    uncertainty = pl.DataFrame(
        [
            {
                "candidate": model,
                "comparator": "raw_micro",
                "unit": "station_day",
                "state": "descriptive_station_cluster_bootstrap",
                "observed_delta_rmse": observed,
                "delta_rmse_ci_low": low,
                "delta_rmse_ci_high": high,
            }
            for model, observed, low, high in (
                ("pooled_micro_ridge", 0.4794439855, -0.35110, 1.13793),
                ("pooled_weather_ridge", 0.5312634770, -0.29861, 1.17681),
            )
        ]
    )
    control_scores = pl.DataFrame(
        {
            "control": ["neighbor_exclusion"],
            "state": ["scored"],
            "value": [0.5312634770],
        }
    )
    control_summary = pl.DataFrame(
        {
            "control": ["neighbor_exclusion"],
            "state": ["complete"],
            "value": [0.5312634770],
        }
    )
    return fold_audit, scores, deltas, uncertainty, control_scores, control_summary


def synthetic_gate_rows(state: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "condition_id": list(audit.FUSION_CONDITION_IDS),
            "state": [state] * len(audit.FUSION_CONDITION_IDS),
            "reason": ["synthetic"] * len(audit.FUSION_CONDITION_IDS),
            "evidence_sha256": ["a" * 64] * len(audit.FUSION_CONDITION_IDS),
            "overall_verdict": ["go" if state == "pass" else "stop"]
            * len(audit.FUSION_CONDITION_IDS),
        }
    )


def assembled_result_fixture() -> audit.AgreementAuditResult:
    fold_audit, scores, deltas, uncertainty, control_scores, control_summary = (
        fusion_gate_fixture()
    )
    gate = audit.evaluate_fusion_gate(
        fold_audit, scores, deltas, uncertainty, control_scores, control_summary
    )
    summary = {
        "primary_station_day_rmse": {
            row["model"]: row["value"]
            for row in scores.iter_rows(named=True)
        },
        "secondary_device_day_delta_rmse": {
            row["model"]: row["value"]
            for row in deltas.filter(pl.col("unit") == "device_day").iter_rows(
                named=True
            )
        },
        "overall_verdict": audit.overall_fusion_verdict(gate),
    }
    result = audit.AgreementAuditResult(
        fold_audit=fold_audit,
        scores=scores,
        deltas=deltas,
        uncertainty=uncertainty,
        control_scores=control_scores,
        control_summary=control_summary,
        fusion_gate=gate,
        summary=summary,
        manifest={"schema_version": 1, "analysis": "micro_sensor_agreement_audit"},
    )
    return replace(
        result,
        manifest={**result.manifest, "generation_sha256": audit.result_identity(result)},
    )


@pytest.fixture
def prepared_audit_result() -> audit.AgreementAuditResult:
    return assembled_result_fixture()


def run_lock_subprocess(path: Path) -> subprocess.CompletedProcess[str]:
    script = (
        "from pathlib import Path\n"
        "from twair.analysis.micro_sensor_agreement_audit import "
        "agreement_audit_run_lock\n"
        "try:\n"
        "    with agreement_audit_run_lock(Path(__import__('sys').argv[1])):\n"
        "        pass\n"
        "except RuntimeError as exc:\n"
        "    print(f'already running: {exc}', file=__import__('sys').stderr)\n"
        "    raise SystemExit(1)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


def interrupted_write(
    _frame: pl.DataFrame, _path: Path, *_args: object, **_kwargs: object
) -> None:
    raise KeyboardInterrupt


def test_shipped_config_pins_the_audit_protocol() -> None:
    config = load_micro_sensor_agreement_audit_config()
    assert config.protocol_revision == 1
    assert config.annual_git_sha == "e4839bc"
    assert config.agreement_git_sha == "b7bff3e"
    assert config.permutation_draws == 999
    assert config.bootstrap_draws == 1999
    assert config.target_time_shifts_days == (7, 14, 28)
    assert config.neighbor_exclusion_buffers_km == (0.5, 1.0, 2.0)


@pytest.mark.parametrize(
    "path",
    [
        "analysis.protocol_revision",
        "analysis.annual_generation_sha256",
        "analysis.annual_manifest_sha256",
        "analysis.agreement_generation_sha256",
        "analysis.agreement_manifest_sha256",
        "analysis.satellite_generation_sha256",
        "analysis.satellite_panel_sha256",
        "analysis.satellite_panel_bytes",
        "analysis.reviewed_geography_sha256",
        "analysis.reviewed_airzone_sha256",
        "analysis.ridge_alpha",
        "analysis.permutation_draws",
        "analysis.bootstrap_draws",
        "analysis.claim_boundary",
    ],
)
def test_config_rejects_each_scientific_drift(path: str) -> None:
    payload = shipped_config_payload()
    mutate_config_path(payload, path)
    with pytest.raises(ConfigError, match=re.escape(path)):
        load_micro_sensor_agreement_audit_config(payload)


def test_frozen_input_loader_accepts_the_exact_fixture(audit_source_fixture: Path) -> None:
    inputs = load_frozen_agreement_audit_inputs(
        audit_source_fixture, synthetic_audit_config(audit_source_fixture)
    )
    assert inputs.agreement_folds.height == 29
    assert inputs.agreement_paired_days.filter(pl.col("reason") == "eligible").height == 36
    assert inputs.satellite_panel.select("source").n_unique() == 3


@pytest.mark.parametrize("mutation", ["manifest_hash", "member_hash", "extra_member", "link"])
def test_frozen_input_loader_rejects_bound_input_mutation(
    audit_source_fixture: Path, mutation: str
) -> None:
    mutate_frozen_fixture(audit_source_fixture, mutation)
    with pytest.raises(RuntimeError, match="frozen input"):
        load_frozen_agreement_audit_inputs(
            audit_source_fixture, synthetic_audit_config(audit_source_fixture)
        )


def test_fold_reconstruction_reports_all_29_states_and_zero_device_overlap(
    loaded_audit_fixture: tuple[FrozenAuditInputs, AgreementAuditConfig],
) -> None:
    inputs, config = loaded_audit_fixture
    fold_audit, memberships = audit.reconstruct_agreement_folds(inputs, config)
    counts = dict(fold_audit.group_by("state").len().iter_rows())
    assert fold_audit.height == 29
    assert counts == {"scored": 5, "unscored_empty_test": 18, "unscored_empty_train": 6}
    assert fold_audit["device_overlap"].max() == 0
    assert memberships.filter(pl.col("role") == "test")["device_id"].null_count() == 0


def test_fold_reconstruction_rejects_changed_airzone_assignment(
    loaded_audit_fixture: tuple[FrozenAuditInputs, AgreementAuditConfig],
) -> None:
    inputs, config = loaded_audit_fixture
    changed = inputs.geography.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit("changed-zone"))
        .otherwise(pl.col("airzone_official"))
        .alias("airzone_official")
    )
    with pytest.raises(RuntimeError, match="air-zone identity"):
        audit.reconstruct_agreement_folds(replace(inputs, geography=changed), config)


def test_core_refits_predict_every_and_only_scored_test_row(
    loaded_audit_fixture: tuple[FrozenAuditInputs, AgreementAuditConfig],
) -> None:
    inputs, config = loaded_audit_fixture
    folds, memberships = audit.reconstruct_agreement_folds(inputs, config)
    predictions = audit.refit_core_candidates(memberships, folds, config)
    expected = int(folds.filter(pl.col("state") == "scored")["test_rows"].sum()) * 3
    assert predictions.height == expected
    assert set(predictions["model"]) == {
        "raw_micro",
        "pooled_micro_ridge",
        "pooled_weather_ridge",
    }


def test_scores_keep_station_day_primary_and_device_day_secondary(
    scored_prediction_fixture: tuple[pl.DataFrame, pl.DataFrame],
) -> None:
    predictions, folds = scored_prediction_fixture
    scores, deltas = audit.score_audit_predictions(predictions, folds)
    assert set(scores["unit"]) == {"station_day", "device_day"}
    assert scores.filter(pl.col("unit") == "station_day")["primary"].all()
    assert not scores.filter(pl.col("unit") == "device_day")["primary"].any()
    unscored = scores.filter(pl.col("state") != "scored")
    assert scores.filter(pl.col("scope") == "fold")["fold"].n_unique() == 29
    assert unscored["value"].null_count() == unscored.height
    assert deltas.filter(pl.col("unit") == "station_day")["membership_sha256"].n_unique() == 1


def test_reproduction_rejects_a_prediction_difference_above_one_e_minus_twelve(
    loaded_audit_fixture: tuple[FrozenAuditInputs, AgreementAuditConfig],
) -> None:
    inputs, config = loaded_audit_fixture
    changed = inputs.agreement_predictions.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.col("y_pred") + 2e-12)
        .otherwise(pl.col("y_pred"))
        .alias("y_pred")
    )
    folds, memberships = audit.reconstruct_agreement_folds(
        replace(inputs, agreement_predictions=changed), config
    )
    with pytest.raises(RuntimeError, match="agreement reproduction mismatch"):
        audit.refit_core_candidates(memberships, folds, config)


def test_audit_source_does_not_import_agreement_producer_scientific_functions() -> None:
    tree = ast.parse(Path(audit.__file__).read_text(encoding="utf-8"))
    prohibited = {
        "assign_agreement_folds",
        "evaluate_annual_agreement",
        "score_annual_agreement_predictions",
        "_agreement_prediction_rows",
        "_agreement_metric_values",
        "_agreement_score_row",
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert prohibited.isdisjoint(imported)


def test_station_cluster_bootstrap_is_deterministic_for_1999_draws() -> None:
    first = audit.station_cluster_bootstrap(
        clustered_prediction_fixture(), bootstrap_pairs(), audit_config()
    )
    second = audit.station_cluster_bootstrap(
        clustered_prediction_fixture(), bootstrap_pairs(), audit_config()
    )
    assert first.equals(second)
    assert set(first["draws"]) == {1999}


def test_station_cluster_bootstrap_resamples_stations_not_device_rows() -> None:
    result = audit.station_cluster_bootstrap(
        duplicated_device_fixture(), bootstrap_pairs(), audit_config()
    )
    assert result["observed_delta_rmse"][0] == pytest.approx(expected_station_equal_delta())


def test_station_cluster_bootstrap_fails_closed_below_two_clusters() -> None:
    with pytest.raises(RuntimeError, match="at least two station clusters"):
        audit.station_cluster_bootstrap(one_station_fixture(), bootstrap_pairs(), audit_config())


def test_station_label_permutation_is_a_date_zone_bijection() -> None:
    station_days, geography = permutation_fixture()
    permuted, issues = audit.permute_station_day_targets(
        station_days, geography, replicate=17, seed=20260829
    )
    keys = ["date", "airzone_official"]
    expected = station_days.join(geography, on="station_name").group_by(*keys).agg(
        pl.col("ground_pm25_mean").sort()
    )
    observed = permuted.group_by(*keys).agg(pl.col("ground_pm25_mean").sort())
    assert observed.sort(*keys).equals(expected.sort(*keys))
    assert issues.is_empty()


def test_station_label_control_emits_exactly_999_deterministic_draws() -> None:
    first = audit.run_station_label_control(control_fixture(), audit_config())
    second = audit.run_station_label_control(control_fixture(), audit_config())
    assert first.equals(second)
    assert first.select("replicate").n_unique() == 999
    assert first["replicate"].min() == 0
    assert first["replicate"].max() == 998


def test_station_label_control_marks_a_singleton_zone_unpermutable() -> None:
    station_days, geography = singleton_zone_fixture()
    _, issues = audit.permute_station_day_targets(
        station_days, geography, replicate=0, seed=20260829
    )
    assert issues.select("state").to_series().to_list() == ["unpermutable_group"]
    assert issues["n_stations"].to_list() == [1]


@pytest.mark.parametrize("shift_days", [7, 14, 28])
def test_target_shift_is_non_circular_and_records_changed_denominators(
    shift_days: int,
) -> None:
    original = target_shift_fixture()
    shifted = audit.shift_station_day_targets(original, shift_days=shift_days)
    assert shifted["date"].min() == original["date"].min()
    assert shifted["source_date"].min() == original["date"].min() + timedelta(days=shift_days)
    assert shifted.height < original.height
    assert shifted["membership_sha256"].n_unique() == 1
    assert shifted["truth_sha256"].n_unique() == 1


def test_empirical_lower_tail_p_includes_observed_and_null_pseudocounts() -> None:
    assert audit.empirical_lower_tail_p(-2.0, np.array([-3.0, -1.0, 0.0])) == pytest.approx(
        0.5
    )


def test_satellite_context_requires_three_sources_for_each_station_month() -> None:
    incomplete = satellite_panel_fixture().filter(pl.col("source") != "s5p_so2")
    with pytest.raises(RuntimeError, match="three satellite sources"):
        audit.build_satellite_context(incomplete, eligible_station_months_fixture())


def test_satellite_permutation_moves_the_three_source_vector_as_one_block() -> None:
    context = distinguishable_satellite_context_fixture()
    permuted = audit.permute_satellite_context(context, replicate=4, seed=20260829)
    original_vectors = set(context.select(*audit.SATELLITE_FEATURES).iter_rows())
    assert set(permuted.select(*audit.SATELLITE_FEATURES).iter_rows()) == original_vectors


def test_satellite_candidate_is_diagnostic_and_compared_with_weather_ridge() -> None:
    scores = audit.run_satellite_context_control(control_fixture(), audit_config())
    observed = scores.filter(pl.col("variant") == "observed_context")
    assert set(observed["model"]) == {"pooled_weather_satellite_ridge"}
    assert set(observed["comparator"]) == {"pooled_weather_ridge"}
    assert set(observed["unit"]) == {"station_day", "device_day"}


def test_density_ridge_uses_only_the_reviewed_count_and_hour_columns() -> None:
    predictions = audit.run_acquisition_density_baselines(control_fixture(), audit_config())
    density = predictions.filter(pl.col("model") == "acquisition_density_ridge")
    assert set(density["model_features"]) == {",".join(audit.DENSITY_FEATURES)}
    assert set(predictions["model"]) == {"training_mean", "acquisition_density_ridge"}


def test_density_ridge_rejects_any_concentration_weather_ground_or_satellite_value() -> None:
    for prohibited in (
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
        "ground_pm25_mean",
        "satellite_value",
    ):
        with pytest.raises(RuntimeError, match="prohibited density feature"):
            audit.validate_density_features((*audit.DENSITY_FEATURES, prohibited))


@pytest.mark.parametrize("buffer_km", [0.5, 1.0, 2.0])
def test_neighbor_exclusion_removes_every_training_device_inside_buffer(
    buffer_km: float,
) -> None:
    membership, geography = neighbor_fixture(buffer_km)
    filtered, evidence = audit.exclude_neighbor_training_rows(
        membership, geography, buffer_km=buffer_km
    )
    assert "inside" not in set(filtered.filter(pl.col("role") == "train")["device_id"])
    assert "outside" in set(filtered.filter(pl.col("role") == "train")["device_id"])
    assert evidence["removed_devices"][0] == 1


def test_neighbor_exclusion_never_changes_test_rows() -> None:
    membership, geography = neighbor_fixture(2.0)
    before = membership.filter(pl.col("role") == "test").sort("date", "device_id")
    after, evidence = audit.exclude_neighbor_training_rows(
        membership, geography, buffer_km=2.0
    )
    assert after.filter(pl.col("role") == "test").sort("date", "device_id").equals(before)
    assert evidence["test_membership_sha256"][0] == membership_hash(before)


def test_neighbor_exclusion_persists_a_new_unestimable_fold_state() -> None:
    membership, geography = all_training_inside_fixture()
    _, evidence = audit.exclude_neighbor_training_rows(
        membership, geography, buffer_km=2.0
    )
    assert evidence["state"][0] == "unscored_empty_train_after_neighbor_exclusion"
    assert evidence["remaining_train_rows"][0] == 0


def test_fusion_gate_has_exactly_seven_unique_conditions() -> None:
    gate = audit.evaluate_fusion_gate(*fusion_gate_fixture())
    assert gate.height == 7
    assert gate["condition_id"].n_unique() == 7
    assert set(gate["condition_id"]) == set(audit.FUSION_CONDITION_IDS)


def test_current_evidence_returns_stop_with_conditions_one_through_six_failed() -> None:
    gate = audit.evaluate_fusion_gate(*fusion_gate_fixture())
    states = dict(gate.select("condition_id", "state").iter_rows())
    assert all(
        states[condition] == "fail" for condition in audit.FUSION_CONDITION_IDS[:6]
    )
    assert states["field_spatial_buffer"] == "unmet"
    assert gate["overall_verdict"].unique().to_list() == ["stop"]


def test_fusion_gate_passes_only_when_every_condition_is_pass() -> None:
    gate = synthetic_gate_rows(state="pass")
    assert audit.overall_fusion_verdict(gate) == "go"
    assert (
        audit.overall_fusion_verdict(
            gate.with_columns(
                pl.when(pl.col("condition_id") == "multi_year_drift")
                .then(pl.lit("unmet"))
                .otherwise(pl.col("state"))
                .alias("state")
            )
        )
        == "stop"
    )


def test_summary_preserves_device_day_improvement_and_station_day_reversal() -> None:
    result = assembled_result_fixture()
    primary = result.summary["primary_station_day_rmse"]
    secondary = result.summary["secondary_device_day_delta_rmse"]
    assert primary["raw_micro"] == pytest.approx(4.189404, abs=1e-6)
    assert primary["pooled_micro_ridge"] == pytest.approx(4.668848, abs=1e-6)
    assert primary["pooled_weather_ridge"] == pytest.approx(4.720668, abs=1e-6)
    assert secondary["pooled_micro_ridge"] < 0.0
    assert secondary["pooled_weather_ridge"] < 0.0
    assert result.summary["overall_verdict"] == "stop"


@pytest.mark.parametrize("changed", ["scores", "control_scores"])
def test_generation_identity_binds_scientific_table_content(changed: str) -> None:
    result = assembled_result_fixture()
    candidate, comparator, delta = (
        audit._normalize_audit_floats(pl.DataFrame({"value": [value]}))["value"][0]
        for value in (4.382554892876076, 4.873557915926531, -0.49100302305045496)
    )
    assert abs(delta - (candidate - comparator)) <= 1e-12
    source = getattr(result, changed)
    subprecision_noise = source.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.col("value") + 1e-15)
        .otherwise(pl.col("value"))
        .alias("value")
    )
    assert audit._normalize_audit_floats(source).equals(
        audit._normalize_audit_floats(subprecision_noise)
    )
    frame = getattr(result, changed).with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.col("value") + 1.0)
        .otherwise(pl.col("value"))
        .alias("value")
    )
    mutated = replace(result, **{changed: frame})
    assert result.manifest["generation_sha256"] != audit.result_identity(mutated)


def test_writer_publishes_the_exact_nine_member_inventory_atomically(
    prepared_audit_result: audit.AgreementAuditResult, tmp_path: Path
) -> None:
    written = audit.write_micro_sensor_agreement_audit_result(
        prepared_audit_result, output_root=tmp_path / "audit"
    )
    assert set(written) == set(audit.AUDIT_MEMBER_NAMES)
    assert {path.name for path in written.values()} == set(audit.AUDIT_MEMBER_NAMES)
    assert json.loads(written["manifest.json"].read_text(encoding="utf-8"))[
        "complete"
    ] is True


def test_writer_reuses_only_a_byte_identical_existing_generation(
    prepared_audit_result: audit.AgreementAuditResult, tmp_path: Path
) -> None:
    first = audit.write_micro_sensor_agreement_audit_result(
        prepared_audit_result, output_root=tmp_path
    )
    second = audit.write_micro_sensor_agreement_audit_result(
        prepared_audit_result, output_root=tmp_path
    )
    assert first == second
    assert {name: path.read_bytes() for name, path in first.items()} == {
        name: path.read_bytes() for name, path in second.items()
    }


def test_writer_never_overwrites_a_mutated_existing_generation(
    prepared_audit_result: audit.AgreementAuditResult, tmp_path: Path
) -> None:
    written = audit.write_micro_sensor_agreement_audit_result(
        prepared_audit_result, output_root=tmp_path
    )
    written["scores.parquet"].write_bytes(b"mutated")
    with pytest.raises(RuntimeError, match="existing generation is not reusable"):
        audit.write_micro_sensor_agreement_audit_result(
            prepared_audit_result, output_root=tmp_path
        )


def test_one_run_lock_rejects_a_second_process(tmp_path: Path) -> None:
    with audit.agreement_audit_run_lock(tmp_path / "audit.lock"):
        completed = run_lock_subprocess(tmp_path / "audit.lock")
    assert completed.returncode != 0
    assert "already running" in completed.stderr


def test_interrupted_staging_leaves_no_final_or_invocation_residue(
    prepared_audit_result: audit.AgreementAuditResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pl.DataFrame, "write_parquet", interrupted_write)
    with pytest.raises(KeyboardInterrupt):
        audit.write_micro_sensor_agreement_audit_result(
            prepared_audit_result, output_root=tmp_path
        )
    assert not list(
        (tmp_path / "generations").glob(
            ".micro-sensor-agreement-audit.staging-*"
        )
    )
    assert not list((tmp_path / "generations").glob("[0-9a-f]" * 64))
