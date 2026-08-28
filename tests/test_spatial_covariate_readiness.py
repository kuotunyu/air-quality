"""Contract tests for frozen spatial covariate readiness inputs."""

from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

import twair.analysis.spatial_covariate_readiness as readiness
from twair.analysis.spatial_covariate_readiness import (
    COVARIATE_MODEL_FEATURES,
    COVARIATE_READINESS_LIMITATIONS,
    CovariateReadinessConfig,
    FrozenInputs,
    InputFile,
    aggregate_era5_monthly,
    assemble_covariates,
    bootstrap_station_delta,
    build_covariate_fold_ledger,
    decide_covariate_gate,
    fit_covariate_model,
    load_frozen_inputs,
    load_spatial_covariate_readiness_config,
    paired_readiness_deltas,
    pivot_satellite_monthly,
    predict_readiness_methods,
    score_readiness_predictions,
)
from twair.analysis.spatial_surface_baseline import SPATIAL_BASELINE_TABLE_SCHEMAS
from twair.config import ConfigError

BASELINE_GENERATION = "620b7ba088906611c191d0f371b5405f8096059cefc488306b6849b64588ef0f"
INVENTORY_GENERATION = "58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788"


def _era5_months(*months: tuple[int, int]) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for year, month in months:
        local_start = datetime(year, month, 1, tzinfo=UTC) - timedelta(hours=8)
        if month == 12:
            local_end = datetime(year + 1, 1, 1, tzinfo=UTC) - timedelta(hours=8)
        else:
            local_end = datetime(year, month + 1, 1, tzinfo=UTC) - timedelta(hours=8)
        for offset in range(int((local_end - local_start).total_seconds() // 3600)):
            rows.append(
                {
                    "station_name": "station-00",
                    "ts_utc": local_start + timedelta(hours=offset),
                    "grid_lat": 23.5,
                    "grid_lon": 120.5,
                    "blh_m": 100.0,
                    "u10_m_s": 3.0,
                    "v10_m_s": 4.0,
                    "t2m_k": 300.0,
                    "d2m_k": 298.0,
                    "sp_pa": 100000.0,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("ts_utc").cast(pl.Datetime(time_zone="UTC")))


def test_aggregate_era5_monthly_uses_complete_asia_taipei_calendar_months() -> None:
    monthly = aggregate_era5_monthly(_era5_months((2024, 1), (2024, 2)), years=(2024, 2025))

    january, february = monthly.iter_rows(named=True)
    assert january["month"] == date(2024, 1, 1)
    assert january["n_hours"] == 31 * 24
    assert january["era5_wind_speed_mean_m_s"] == pytest.approx(5.0)
    assert january["era5_dewpoint_depression_mean_k"] == pytest.approx(2.0)
    assert february["month"] == date(2024, 2, 1)
    assert february["n_hours"] == 29 * 24


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda frame: pl.concat([frame, frame.head(1)]), "duplicate"),
        (lambda frame: frame.slice(1), "complete"),
        (
            lambda frame: frame.with_columns(
                pl.when(pl.int_range(pl.len()) == 0)
                .then(pl.lit(None))
                .otherwise(pl.col("u10_m_s"))
                .alias("u10_m_s")
            ),
            "source",
        ),
        (
            lambda frame: frame.with_columns(
                pl.when(pl.int_range(pl.len()) == 0)
                .then(pl.lit(float("nan")))
                .otherwise(pl.col("u10_m_s"))
                .alias("u10_m_s")
            ),
            "source",
        ),
        (
            lambda frame: frame.with_columns(
                pl.when(pl.int_range(pl.len()) == 0)
                .then(pl.lit(24.0))
                .otherwise(pl.col("grid_lat"))
                .alias("grid_lat")
            ),
            "grid",
        ),
        (
            lambda frame: pl.concat(
                [
                    frame,
                    pl.DataFrame(
                        {
                            "station_name": ["station-00"],
                            "ts_utc": [datetime(2023, 12, 1, tzinfo=UTC)],
                            "grid_lat": [23.5],
                            "grid_lon": [120.5],
                            "blh_m": [100.0],
                            "u10_m_s": [3.0],
                            "v10_m_s": [4.0],
                            "t2m_k": [300.0],
                            "d2m_k": [298.0],
                            "sp_pa": [100000.0],
                        }
                    ).with_columns(pl.col("ts_utc").cast(pl.Datetime(time_zone="UTC"))),
                ]
            ),
            "outside",
        ),
    ],
)
def test_aggregate_era5_monthly_rejects_invalid_station_hour_input(
    mutate: Any, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        aggregate_era5_monthly(mutate(_era5_months((2024, 1))), years=(2024, 2025))


def test_aggregate_era5_monthly_rejects_an_off_grid_timestamp_that_replaces_a_local_hour() -> None:
    frame = _era5_months((2024, 1)).with_columns(
        pl.when(pl.col("ts_utc") == datetime(2023, 12, 31, 16, tzinfo=UTC))
        .then(
            pl.lit(datetime(2023, 12, 31, 16, 30, tzinfo=UTC), dtype=pl.Datetime(time_zone="UTC"))
        )
        .otherwise(pl.col("ts_utc"))
        .alias("ts_utc")
    )

    with pytest.raises(RuntimeError, match="complete local calendar hours"):
        aggregate_era5_monthly(frame, years=(2024, 2025))


def _satellite_long(panel: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for row in panel.iter_rows(named=True):
        for source, value in (("maiac_aod", None), ("s5p_no2", 2.0), ("s5p_so2", 3.0)):
            observed = value is not None
            target_state = row["target_state"]
            rows.append(
                {
                    "source": source,
                    "station_name": row["station_name"],
                    "month": row["month"],
                    "satellite_value": value,
                    "ground_value": row["mean"],
                    "satellite_observed": observed,
                    "ground_row_present": True,
                    "ground_meets_threshold": target_state == "observed",
                    "ground_observed": target_state == "observed",
                    "ground_withheld": target_state == "withheld",
                    "pair_observed": observed and target_state == "observed",
                }
            )
    return pl.DataFrame(rows, schema_overrides={"month": pl.Date})


def _covariate_inputs(
    tmp_path: Path,
) -> tuple[FrozenInputs, CovariateReadinessConfig, pl.DataFrame]:
    month = date(2024, 3, 1)
    panel = pl.DataFrame(
        {
            "station_name": ["station-00", "station-01"],
            "station_type_official": ["一般站", "一般站"],
            "lon": [120.0, 120.1],
            "lat": [23.0, 23.1],
            "month": [month, month],
            "pollutant": ["PM2.5", "PM2.5"],
            "mean": [10.0, None],
            "meets_threshold": [True, False],
            "target_state": ["observed", "withheld"],
        },
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["panel"],
    )
    support = pl.DataFrame(
        {
            "station_name": ["station-00", "station-01"],
            "nearest_station": ["station-01", "station-00"],
            "nearest_station_km": [1.0, 1.0],
            "stations_within_20km": [1, 1],
            "stations_within_40km": [1, 1],
            "x_m": [100.0, 200.0],
            "y_m": [300.0, 400.0],
        },
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["support"],
    )
    stations = panel.select("station_name", "station_type_official", "lon", "lat").unique(
        maintain_order=True
    )
    era5 = pl.concat(
        [
            _era5_months((2024, 3)),
            _era5_months((2024, 3)).with_columns(pl.lit("station-01").alias("station_name")),
        ]
    )
    satellite = _satellite_long(panel)
    era5_path = tmp_path / "interim" / "era5" / "era5_station_hour.parquet"
    satellite_path = tmp_path / "outputs" / "m8_satellite" / "panel.parquet"
    era5_path.parent.mkdir(parents=True)
    satellite_path.parent.mkdir(parents=True)
    era5.write_parquet(era5_path)
    satellite.write_parquet(satellite_path)
    files = tuple(InputFile(path=path, bytes=0, sha256="") for path in (era5_path, satellite_path))
    return (
        FrozenInputs(
            stations=stations,
            panel=panel,
            support=support,
            baseline_folds=pl.DataFrame(),
            input_files=files,
            baseline_generation_sha256=BASELINE_GENERATION,
            station_inventory_generation_sha256=INVENTORY_GENERATION,
        ),
        _config(),
        satellite,
    )


def test_pivot_satellite_monthly_preserves_aod_nulls() -> None:
    panel = pl.DataFrame(
        {
            "station_name": ["station-00"],
            "month": [date(2024, 3, 1)],
            "mean": [10.0],
            "target_state": ["observed"],
        }
    )

    pivoted = pivot_satellite_monthly(_satellite_long(panel))

    assert pivoted["maiac_aod"].to_list() == [None]
    assert pivoted["s5p_no2"].to_list() == [2.0]
    assert pivoted["s5p_so2"].to_list() == [3.0]


def test_assemble_covariates_preserves_authoritative_panel_keys_and_satellite_nulls(
    tmp_path: Path,
) -> None:
    inputs, config, _ = _covariate_inputs(tmp_path)

    result = assemble_covariates(inputs, config)

    assert (
        result.select("station_name", "month").rows()
        == inputs.panel.select("station_name", "month").sort("station_name", "month").rows()
    )
    assert result.group_by("target_state").len().sort("target_state").rows() == [
        ("observed", 1),
        ("withheld", 1),
    ]
    assert result.filter(pl.col("station_name") == "station-00")["maiac_aod"][0] is None
    march = result.filter(pl.col("month") == date(2024, 3, 1))
    assert march["month_sin"][0] == pytest.approx(1.0)
    assert march["month_cos"][0] == pytest.approx(0.0, abs=1e-12)


def test_assemble_covariates_ignores_complete_non_cohort_external_stations(
    tmp_path: Path,
) -> None:
    inputs, config, satellite = _covariate_inputs(tmp_path)
    months = tuple((year, month) for year in (2024, 2025) for month in range(1, 13))
    extra_era5 = _era5_months(*months).with_columns(pl.lit("station-extra").alias("station_name"))
    extra_panel = pl.DataFrame(
        {
            "station_name": ["station-extra"] * len(months),
            "month": [date(year, month, 1) for year, month in months],
            "mean": [10.0] * len(months),
            "target_state": ["observed"] * len(months),
        },
        schema_overrides={"month": pl.Date},
    )
    era5_path = next(item.path for item in inputs.input_files if "era5" in item.path.parts)
    satellite_path = next(
        item.path for item in inputs.input_files if "m8_satellite" in item.path.parts
    )
    pl.concat([pl.read_parquet(era5_path), extra_era5]).write_parquet(era5_path)
    pl.concat([satellite, _satellite_long(extra_panel)]).write_parquet(satellite_path)

    result = assemble_covariates(inputs, config)

    assert (
        result.select("station_name", "month").rows()
        == inputs.panel.select("station_name", "month").sort("station_name", "month").rows()
    )
    assert "station-extra" not in result["station_name"].to_list()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda frame: pl.concat([frame, frame.head(1)]), "duplicate"),
        (
            lambda frame: frame.with_columns(
                pl.when(pl.col("source") == "s5p_so2")
                .then(pl.lit("unexpected"))
                .otherwise(pl.col("source"))
                .alias("source")
            ),
            "source",
        ),
        (
            lambda frame: frame.with_columns(
                pl.when(pl.col("source") == "s5p_no2")
                .then(pl.lit(False))
                .otherwise(pl.col("satellite_observed"))
                .alias("satellite_observed")
            ),
            "satellite observed",
        ),
        (lambda frame: frame.filter(pl.col("source") != "s5p_so2"), "complete source"),
    ],
)
def test_pivot_satellite_monthly_rejects_invalid_long_rows(mutate: Any, message: str) -> None:
    panel = pl.DataFrame(
        {
            "station_name": ["station-00"],
            "month": [date(2024, 3, 1)],
            "mean": [10.0],
            "target_state": ["observed"],
        }
    )

    with pytest.raises(RuntimeError, match=message):
        pivot_satellite_monthly(mutate(_satellite_long(panel)))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda frame: frame.with_columns(
                pl.when(pl.col("station_name") == "station-00")
                .then(pl.lit("station-malformed"))
                .otherwise(pl.col("station_name"))
                .alias("station_name")
            ),
            "satellite keys",
        ),
        (
            lambda frame: frame.filter(pl.col("station_name") != "station-00"),
            "satellite keys",
        ),
        (
            lambda frame: frame.with_columns(
                pl.when(pl.col("source") == "s5p_no2")
                .then(pl.lit(False))
                .otherwise(pl.col("ground_observed"))
                .alias("ground_observed")
            ),
            "ground state",
        ),
    ],
)
def test_assemble_covariates_rejects_satellite_drift(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    inputs, config, satellite = _covariate_inputs(tmp_path)
    satellite_path = inputs.input_files[-1].path
    mutate(satellite).write_parquet(satellite_path)

    with pytest.raises(RuntimeError, match=message):
        assemble_covariates(inputs, config)


def _config_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "analysis": {
            "years": [2024, 2025],
            "baseline_generation_sha256": "a" * 64,
            "station_inventory_generation_sha256": "b" * 64,
            "minimum_train_stations": 8,
        },
        "methods": {
            "comparator": "idw2",
            "candidates": ["covariate_gbm", "covariate_gbm_idw2"],
            "idw_power": 2.0,
            "minimum_distance_km": 0.1,
            "model": {
                "n_estimators": 200,
                "learning_rate": 0.05,
                "num_leaves": 31,
                "min_child_samples": 10,
                "n_jobs": 1,
                "seed": 20260811,
            },
        },
        "validation": {
            "evaluations": ["buffer_20km", "buffer_40km", "spatial_cluster"],
            "bootstrap_draws": 9999,
            "bootstrap_seed": 20260828,
        },
    }


def _config() -> CovariateReadinessConfig:
    payload = _config_payload()
    analysis = payload["analysis"]
    assert isinstance(analysis, dict)
    analysis["baseline_generation_sha256"] = BASELINE_GENERATION
    analysis["station_inventory_generation_sha256"] = INVENTORY_GENERATION
    return load_spatial_covariate_readiness_config(payload)


def _model_fixture() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    station_names = tuple(f"s{index}" for index in range(6))
    x_by_station = {
        "s0": 0.0,
        "s1": 10_000.0,
        "s2": 20_000.0,
        "s3": 30_000.0,
        "s4": 50_000.0,
        "s5": 80_000.0,
    }
    cluster_by_station = {station: index // 2 for index, station in enumerate(station_names)}
    months = [date(year, month, 1) for year in (2024, 2025) for month in range(1, 13)]
    covariate_rows: list[dict[str, object]] = []
    for station_index, station_name in enumerate(station_names):
        for month in months:
            withheld = station_name == "s5" and month == date(2025, 5, 1)
            covariate_rows.append(
                {
                    "station_name": station_name,
                    "month": month,
                    "target_state": "withheld" if withheld else "observed",
                    "PM2.5": None
                    if withheld
                    else float(1000 * (month.year - 2024) + 100 * month.month + station_index),
                    "lon": 120.0 + station_index * 0.1,
                    "lat": 23.0,
                    "x_m": x_by_station[station_name],
                    "y_m": 0.0,
                    "month_sin": math.sin(2.0 * math.pi * month.month / 12.0),
                    "month_cos": math.cos(2.0 * math.pi * month.month / 12.0),
                    "era5_blh_mean_m": 100.0 + month.month,
                    "era5_u10_mean_m_s": 3.0,
                    "era5_v10_mean_m_s": 4.0,
                    "era5_wind_speed_mean_m_s": 5.0,
                    "era5_t2m_mean_k": 290.0 + month.month,
                    "era5_dewpoint_depression_mean_k": 2.0,
                    "era5_sp_mean_pa": 100_000.0,
                    "maiac_aod": None
                    if station_name == "s1" and month == date(2024, 1, 1)
                    else 0.1 + station_index / 100,
                    "s5p_no2": 0.2 + station_index / 100,
                    "s5p_so2": 0.3 + station_index / 100,
                }
            )
    covariates = pl.DataFrame(covariate_rows).with_columns(
        pl.col("month").cast(pl.Date),
        pl.col("PM2.5").cast(pl.Float64),
        pl.col("maiac_aod").cast(pl.Float64),
    )
    support = pl.DataFrame(
        {
            "station_name": station_names,
            "nearest_station": ("s1", "s0", "s1", "s2", "s3", "s4"),
            "nearest_station_km": (10.0, 10.0, 10.0, 10.0, 20.0, 30.0),
            "stations_within_20km": (2, 3, 4, 3, 1, 0),
            "stations_within_40km": (3, 4, 5, 5, 4, 2),
            "x_m": tuple(x_by_station[name] for name in station_names),
            "y_m": (0.0,) * 6,
        },
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["support"],
    )
    observed_by_month = {
        month: [name for name in station_names if not (name == "s5" and month == date(2025, 5, 1))]
        for month in months
    }
    fold_rows: list[dict[str, object]] = []
    for row in covariates.iter_rows(named=True):
        target_station = str(row["station_name"])
        month = row["month"]
        target_x = x_by_station[target_station]
        target_cluster = cluster_by_station[target_station]
        for evaluation in ("buffer_20km", "buffer_40km", "spatial_cluster"):
            if evaluation == "spatial_cluster":
                allowed = [
                    name
                    for name in observed_by_month[month]
                    if cluster_by_station[name] != target_cluster
                ]
            else:
                radius_m = 20_000.0 if evaluation == "buffer_20km" else 40_000.0
                allowed = [
                    name
                    for name in observed_by_month[month]
                    if name != target_station and abs(x_by_station[name] - target_x) > radius_m
                ]
            target_state = str(row["target_state"])
            fold_rows.append(
                {
                    "evaluation": evaluation,
                    "fold_id": f"{evaluation}:{target_station}",
                    "year": month.year,
                    "month": month,
                    "target_station": target_station,
                    "target_cluster": target_cluster,
                    "target_state": target_state,
                    "observed": row["PM2.5"],
                    "train_stations": sorted(allowed),
                    "n_train": len(allowed),
                    "fold_state": (
                        "eligible" if target_state == "observed" else "unscored_target_withheld"
                    ),
                    "fold_reason": (
                        None if target_state == "observed" else "target_state=withheld"
                    ),
                }
            )
    folds = pl.DataFrame(fold_rows, schema=SPATIAL_BASELINE_TABLE_SCHEMAS["folds"])
    return covariates.sort("station_name", "month"), support, folds


def _model_config(*, minimum_train_stations: int = 1) -> CovariateReadinessConfig:
    return replace(_config(), minimum_train_stations=minimum_train_stations)


def test_covariate_fold_ledger_preserves_same_year_and_forward_target_keys() -> None:
    covariates, support, baseline_folds = _model_fixture()

    ledger = build_covariate_fold_ledger(covariates, support, baseline_folds, _model_config())

    target_key = [
        "evaluation",
        "training_period",
        "train_year",
        "target_year",
        "month",
        "target_station",
    ]
    assert ledger.height == (3 * 6 * 24) + (3 * 6 * 12)
    assert ledger.select(target_key).n_unique() == ledger.height
    assert set(ledger["training_period"]) == {"same_year", "2024_to_2025"}
    same_year = ledger.filter(pl.col("training_period") == "same_year")
    assert same_year.filter(pl.col("train_year") != pl.col("target_year")).is_empty()
    assert (
        same_year.group_by("evaluation", "target_year")
        .agg(pl.struct("month", "target_station").n_unique().alias("keys"))["keys"]
        .to_list()
        == [72] * 6
    )
    forward = ledger.filter(pl.col("training_period") == "2024_to_2025")
    assert set(forward["train_year"]) == {2024}
    assert set(forward["target_year"]) == {2025}
    assert forward.group_by("evaluation").len()["len"].to_list() == [72, 72, 72]
    withheld = ledger.filter(
        (pl.col("target_station") == "s5") & (pl.col("month") == date(2025, 5, 1))
    )
    assert withheld.height == 6
    assert set(withheld["fold_state"]) == {"unscored_target_withheld"}


def test_held_location_ledger_excludes_every_forbidden_station_for_the_full_year() -> None:
    covariates, support, baseline_folds = _model_fixture()

    ledger = build_covariate_fold_ledger(covariates, support, baseline_folds, _model_config())

    coordinates = dict(support.select("station_name", "x_m").iter_rows())
    clusters = (
        baseline_folds.select("target_station", "target_cluster")
        .unique()
        .rows_by_key("target_station", unique=True)
    )
    for row in ledger.iter_rows(named=True):
        target = str(row["target_station"])
        train_stations = [str(name) for name in row["train_stations"]]
        assert train_stations == sorted(train_stations)
        assert target not in train_stations
        if row["evaluation"].startswith("buffer_"):
            radius_km = 20.0 if row["evaluation"] == "buffer_20km" else 40.0
            assert all(
                abs(coordinates[name] - coordinates[target]) / 1000 > radius_km
                for name in train_stations
            )
        else:
            assert all(clusters[name][0] != clusters[target][0] for name in train_stations)
        model_rows = covariates.filter(
            (pl.col("month").dt.year() == row["train_year"])
            & pl.col("station_name").is_in(train_stations)
            & (pl.col("target_state") == "observed")
        )
        source_month = date(row["train_year"], row["month"].month, 1)
        same_month_rows = model_rows.filter(pl.col("month") == source_month)
        assert row["n_model_train_rows"] == model_rows.height
        assert row["n_same_month_train_rows"] == same_month_rows.height


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate_target", "duplicate"),
        ("wrong_coordinate", "coordinate"),
        ("null_coordinate", "coordinate"),
        ("cluster_membership", "cluster"),
        ("forbidden_train_station", "train station"),
    ],
)
def test_covariate_fold_ledger_rejects_mutated_authoritative_inputs(
    mutation: str, message: str
) -> None:
    covariates, support, baseline_folds = _model_fixture()
    if mutation == "duplicate_target":
        covariates = pl.concat([covariates, covariates.head(1)])
    elif mutation == "wrong_coordinate":
        covariates = covariates.with_columns(
            pl.when(pl.col("station_name") == "s0")
            .then(pl.col("x_m") + 1.0)
            .otherwise(pl.col("x_m"))
            .alias("x_m")
        )
    elif mutation == "null_coordinate":
        covariates = covariates.with_columns(
            pl.when(pl.col("station_name") == "s0")
            .then(pl.lit(None, dtype=pl.Float64))
            .otherwise(pl.col("x_m"))
            .alias("x_m")
        )
    elif mutation == "cluster_membership":
        changed = (
            (pl.col("evaluation") == "spatial_cluster")
            & (pl.col("target_station") == "s0")
            & (pl.col("month") == date(2024, 1, 1))
        )
        baseline_folds = baseline_folds.with_columns(
            pl.when(changed)
            .then(pl.col("target_cluster") + 1)
            .otherwise(pl.col("target_cluster"))
            .alias("target_cluster")
        )
    else:
        assert mutation == "forbidden_train_station"
        changed = (
            (pl.col("evaluation") == "buffer_20km")
            & (pl.col("target_station") == "s0")
            & (pl.col("month") == date(2024, 1, 1))
        )
        baseline_folds = baseline_folds.with_columns(
            pl.when(changed)
            .then(pl.lit(["s0", "s3", "s4", "s5"]))
            .otherwise(pl.col("train_stations"))
            .alias("train_stations"),
            pl.when(changed).then(pl.lit(4)).otherwise(pl.col("n_train")).alias("n_train"),
        )

    with pytest.raises(RuntimeError, match=message):
        build_covariate_fold_ledger(covariates, support, baseline_folds, _model_config())


def test_fit_covariate_model_uses_only_fixed_features_and_preserves_satellite_nulls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covariates, _, _ = _model_fixture()
    train = covariates.filter(
        (pl.col("month").dt.year() == 2024) & (pl.col("target_state") == "observed")
    ).with_columns(
        pl.lit(999.0).alias("PM10"),
        pl.lit("forbidden-fold-state").alias("fold_state"),
    )
    target = train.filter((pl.col("station_name") == "s0") & (pl.col("month") == date(2024, 2, 1)))
    captured: dict[str, Any] = {}

    class RecordingRegressor:
        def __init__(self, **parameters: object) -> None:
            captured["parameters"] = parameters

        def fit(self, features: np.ndarray, truth: np.ndarray) -> RecordingRegressor:
            captured["fit_features"] = features
            captured["truth"] = truth
            return self

        def predict(self, features: np.ndarray) -> np.ndarray:
            captured["predict_features"] = features
            return np.zeros(features.shape[0], dtype=float)

    monkeypatch.setattr(
        readiness,
        "_lgbm_regressor",
        lambda **parameters: RecordingRegressor(**parameters),
    )

    predicted = fit_covariate_model(train, target, _model_config())

    assert COVARIATE_MODEL_FEATURES == (
        "x_m",
        "y_m",
        "month_sin",
        "month_cos",
        "era5_blh_mean_m",
        "era5_u10_mean_m_s",
        "era5_v10_mean_m_s",
        "era5_wind_speed_mean_m_s",
        "era5_t2m_mean_k",
        "era5_dewpoint_depression_mean_k",
        "era5_sp_mean_pa",
        "maiac_aod",
        "s5p_no2",
        "s5p_so2",
    )
    assert captured["fit_features"].shape == (72, 14)
    assert captured["truth"].shape == (72,)
    assert captured["predict_features"].shape == (1, 14)
    assert np.isnan(captured["fit_features"]).sum() == 1
    assert predicted.tolist() == [0.0]
    assert captured["parameters"] == {
        "n_estimators": 200,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 10,
        "subsample": 0.8,
        "subsample_freq": 1,
        "colsample_bytree": 0.8,
        "n_jobs": 1,
        "random_state": 20260811,
        "verbose": -1,
    }


def test_real_covariate_model_accepts_a_null_satellite_feature() -> None:
    covariates, _, _ = _model_fixture()
    train = covariates.filter(
        (pl.col("month").dt.year() == 2024) & (pl.col("target_state") == "observed")
    )
    target = (
        covariates.filter((pl.col("station_name") == "s1") & (pl.col("month") == date(2025, 1, 1)))
        .with_columns(pl.lit(None, dtype=pl.Float64).alias("maiac_aod"))
        .head(1)
    )

    predicted = fit_covariate_model(train, target, _model_config())

    assert predicted.shape == (1,)
    assert np.isfinite(predicted).all()


def _deterministic_model(
    calls: list[tuple[pl.DataFrame, pl.DataFrame]],
) -> Any:
    def fit(
        train: pl.DataFrame,
        predict: pl.DataFrame,
        _config: CovariateReadinessConfig,
    ) -> np.ndarray:
        calls.append((train, predict))
        return np.asarray(predict["x_m"].to_numpy(), dtype=float) / 10_000.0

    return fit


def test_predictions_use_authorized_same_year_and_forward_month_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covariates, support, baseline_folds = _model_fixture()
    config = _model_config()
    ledger = build_covariate_fold_ledger(covariates, support, baseline_folds, config).filter(
        (pl.col("evaluation") == "buffer_20km")
        & (pl.col("target_station") == "s0")
        & pl.col("month").is_in([date(2025, 1, 1), date(2025, 5, 1)])
    )
    calls: list[tuple[pl.DataFrame, pl.DataFrame]] = []
    monkeypatch.setattr(readiness, "fit_covariate_model", _deterministic_model(calls))

    predictions = predict_readiness_methods(covariates, ledger, config)

    assert predictions.height == ledger.height * 3
    assert (
        predictions.group_by(
            "evaluation",
            "training_period",
            "train_year",
            "target_year",
            "month",
            "target_station",
        )
        .agg(pl.col("method").sort())
        .select("method")
        .to_series()
        .to_list()
        == [["covariate_gbm", "covariate_gbm_idw2", "idw2"]] * ledger.height
    )
    assert len(calls) == 2
    assert {tuple(train["month"].dt.year().unique()) for train, _ in calls} == {
        (2024,),
        (2025,),
    }
    assert all("s0" not in set(train["station_name"]) for train, _ in calls)
    assert all(set(train["target_state"]) == {"observed"} for train, _ in calls)

    same_year_january = predictions.filter(
        (pl.col("training_period") == "same_year") & (pl.col("month") == date(2025, 1, 1))
    )
    forward_january = predictions.filter(
        (pl.col("training_period") == "2024_to_2025") & (pl.col("month") == date(2025, 1, 1))
    )
    same_idw = same_year_january.filter(pl.col("method") == "idw2")["predicted"].item()
    forward_idw = forward_january.filter(pl.col("method") == "idw2")["predicted"].item()
    assert 1103.0 <= same_idw <= 1105.0
    assert 103.0 <= forward_idw <= 105.0
    same_residual = same_year_january.filter(pl.col("method") == "covariate_gbm_idw2")[
        "predicted"
    ].item()
    forward_residual = forward_january.filter(pl.col("method") == "covariate_gbm_idw2")[
        "predicted"
    ].item()
    assert 1097.0 <= same_residual <= 1100.0
    assert 97.0 <= forward_residual <= 100.0


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        ("non_finite", "non_finite_prediction"),
        ("wrong_length", "wrong_prediction_length"),
        ("wrong_length_list", "wrong_prediction_length"),
    ],
)
def test_model_output_failures_are_explicit_and_never_fall_back(
    monkeypatch: pytest.MonkeyPatch, failure: str, expected_state: str
) -> None:
    covariates, support, baseline_folds = _model_fixture()
    config = _model_config()
    ledger = (
        build_covariate_fold_ledger(covariates, support, baseline_folds, config)
        .filter(
            (pl.col("evaluation") == "buffer_20km")
            & (pl.col("training_period") == "same_year")
            & (pl.col("target_station") == "s0")
            & (pl.col("month") == date(2024, 1, 1))
        )
        .head(1)
    )

    def broken_model(
        _train: pl.DataFrame,
        predict: pl.DataFrame,
        _config: CovariateReadinessConfig,
    ) -> np.ndarray:
        if failure == "wrong_length_list":
            return [0.0] * (predict.height - 1)  # type: ignore[return-value]
        if failure == "wrong_length":
            return np.zeros(predict.height - 1, dtype=float)
        values = np.zeros(predict.height, dtype=float)
        values[-1] = np.nan
        return values

    monkeypatch.setattr(readiness, "fit_covariate_model", broken_model)

    predictions = predict_readiness_methods(covariates, ledger, config)

    comparator = predictions.filter(pl.col("method") == "idw2").row(0, named=True)
    assert comparator["prediction_state"] == "scored"
    assert comparator["predicted"] is not None
    candidates = predictions.filter(pl.col("method") != "idw2")
    assert set(candidates["prediction_state"]) == {expected_state}
    assert candidates["predicted"].null_count() == 2
    assert candidates["error"].null_count() == 2


def test_forward_prediction_rejects_ledger_metadata_that_would_admit_target_year_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covariates, support, baseline_folds = _model_fixture()
    config = _model_config()
    ledger = build_covariate_fold_ledger(covariates, support, baseline_folds, config).filter(
        (pl.col("evaluation") == "spatial_cluster")
        & (pl.col("training_period") == "2024_to_2025")
        & (pl.col("target_station") == "s5")
    )
    ledger = ledger.with_columns(pl.lit(2025).alias("train_year"))
    calls: list[tuple[pl.DataFrame, pl.DataFrame]] = []
    monkeypatch.setattr(readiness, "fit_covariate_model", _deterministic_model(calls))

    with pytest.raises(RuntimeError, match="training period"):
        predict_readiness_methods(covariates, ledger, config)

    assert calls == []


def test_too_few_same_month_residual_stations_fails_without_pure_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covariates, support, baseline_folds = _model_fixture()
    config = _model_config(minimum_train_stations=3)
    ledger = (
        build_covariate_fold_ledger(covariates, support, baseline_folds, config)
        .filter(
            (pl.col("evaluation") == "buffer_20km")
            & (pl.col("training_period") == "same_year")
            & (pl.col("target_station") == "s0")
            & (pl.col("month") == date(2025, 5, 1))
        )
        .head(1)
    )
    calls: list[tuple[pl.DataFrame, pl.DataFrame]] = []
    monkeypatch.setattr(readiness, "fit_covariate_model", _deterministic_model(calls))

    predictions = predict_readiness_methods(covariates, ledger, config)

    pure = predictions.filter(pl.col("method") == "covariate_gbm").row(0, named=True)
    residual = predictions.filter(pl.col("method") == "covariate_gbm_idw2").row(0, named=True)
    assert pure["prediction_state"] == "scored"
    assert pure["predicted"] == 0.0
    assert residual["prediction_state"] == "insufficient_residual_stations"
    assert residual["predicted"] is None
    assert residual["error"] is None


def test_withheld_target_emits_null_prediction_and_error_for_every_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covariates, support, baseline_folds = _model_fixture()
    config = _model_config()
    ledger = build_covariate_fold_ledger(covariates, support, baseline_folds, config).filter(
        (pl.col("evaluation") == "spatial_cluster")
        & (pl.col("target_station") == "s5")
        & (pl.col("month") == date(2025, 5, 1))
    )

    def must_not_fit(*_args: object, **_kwargs: object) -> np.ndarray:
        raise AssertionError("withheld target must not trigger model fitting")

    monkeypatch.setattr(readiness, "fit_covariate_model", must_not_fit)

    predictions = predict_readiness_methods(covariates, ledger, config)

    assert predictions.height == 6
    assert set(predictions["method"]) == set(config.methods)
    assert set(predictions["prediction_state"]) == {"unscored_target_withheld"}
    assert predictions["predicted"].null_count() == predictions.height
    assert predictions["error"].null_count() == predictions.height


def _score_prediction_fixture() -> pl.DataFrame:
    """Return two periods over the same two-station, two-month target grid."""
    rows: list[dict[str, object]] = []
    errors = {
        "idw2": {"s0": (2.0, 2.0), "s1": (4.0, None)},
        "covariate_gbm": {"s0": (1.0, -1.0), "s1": (5.0, None)},
        "covariate_gbm_idw2": {"s0": (0.5, -0.5), "s1": (1.5, None)},
    }
    for training_period, train_year in (("same_year", 2025), ("2024_to_2025", 2024)):
        for station_index, station in enumerate(("s0", "s1")):
            for month_index, month_number in enumerate((1, 2)):
                month = date(2025, month_number, 1)
                withheld = station == "s1" and month_number == 2
                observed = None if withheld else float(20 + station_index + month_index)
                for method in _model_config().methods:
                    error = errors[method][station][month_index]
                    predicted = None if observed is None or error is None else observed + error
                    rows.append(
                        {
                            "evaluation": "buffer_20km",
                            "training_period": training_period,
                            "train_year": train_year,
                            "target_year": 2025,
                            "month": month,
                            "target_station": station,
                            "target_state": "withheld" if withheld else "observed",
                            "observed": observed,
                            "fold_state": "unscored_target_withheld" if withheld else "eligible",
                            "method": method,
                            "predicted": predicted,
                            "prediction_state": (
                                "unscored_target_withheld" if withheld else "scored"
                            ),
                            "error": error,
                        }
                    )
    return pl.DataFrame(rows).cast(
        {
            "evaluation": pl.String,
            "training_period": pl.String,
            "train_year": pl.Int64,
            "target_year": pl.Int64,
            "month": pl.Date,
            "target_station": pl.String,
            "target_state": pl.String,
            "observed": pl.Float64,
            "fold_state": pl.String,
            "method": pl.String,
            "predicted": pl.Float64,
            "prediction_state": pl.String,
            "error": pl.Float64,
        }
    )


def test_scores_use_authoritative_denominators_and_station_equal_aggregation() -> None:
    scores = score_readiness_predictions(_score_prediction_fixture(), _model_config())

    assert scores.columns == [
        "evaluation",
        "training_period",
        "train_year",
        "target_year",
        "method",
        "n_intended",
        "n_scored",
        "n_failed",
        "n_stations_intended",
        "n_stations_scored",
        "station_clustered_mae",
        "station_clustered_rmse",
        "station_clustered_bias",
        "score_state",
    ]
    row = scores.filter(
        (pl.col("training_period") == "same_year") & (pl.col("method") == "covariate_gbm")
    ).row(0, named=True)
    assert row["n_intended"] == 3
    assert row["n_scored"] == 3
    assert row["n_failed"] == 0
    assert row["n_stations_intended"] == 2
    assert row["n_stations_scored"] == 2
    assert row["station_clustered_mae"] == 3.0
    assert row["station_clustered_rmse"] == 3.0
    assert row["station_clustered_bias"] == 2.5
    assert row["score_state"] == "complete"


@pytest.mark.parametrize("mutation", ["missing_method", "duplicate_key", "fold_state", "nan"])
def test_score_denominator_never_shrinks_under_prediction_mutations(mutation: str) -> None:
    predictions = _score_prediction_fixture()
    changed = (
        (pl.col("training_period") == "same_year")
        & (pl.col("method") == "covariate_gbm")
        & (pl.col("target_station") == "s0")
        & (pl.col("month") == date(2025, 1, 1))
    )
    if mutation == "missing_method":
        predictions = predictions.filter(~changed)
    elif mutation == "duplicate_key":
        predictions = pl.concat([predictions, predictions.filter(changed)])
    elif mutation == "fold_state":
        predictions = predictions.with_columns(
            pl.when(changed)
            .then(pl.lit("unscored_insufficient_train"))
            .otherwise(pl.col("fold_state"))
            .alias("fold_state")
        )
    else:
        assert mutation == "nan"
        predictions = predictions.with_columns(
            pl.when(changed).then(pl.lit(float("nan"))).otherwise(pl.col("error")).alias("error")
        )

    if mutation in {"duplicate_key", "fold_state"}:
        with pytest.raises(RuntimeError, match=r"duplicate|eligibility"):
            score_readiness_predictions(predictions, _model_config())
        return
    row = (
        score_readiness_predictions(predictions, _model_config())
        .filter((pl.col("training_period") == "same_year") & (pl.col("method") == "covariate_gbm"))
        .row(0, named=True)
    )
    assert row["n_intended"] == 3
    assert row["n_scored"] == 2
    assert row["n_failed"] == 1
    assert row["score_state"] in {"missing_intended_predictions", "incomplete_predictions"}


def test_score_rejects_a_missing_withheld_method_row_from_the_full_key_grid() -> None:
    predictions = _score_prediction_fixture().filter(
        ~(
            (pl.col("training_period") == "same_year")
            & (pl.col("method") == "covariate_gbm")
            & (pl.col("target_station") == "s1")
            & (pl.col("month") == date(2025, 2, 1))
        )
    )

    row = (
        score_readiness_predictions(predictions, _model_config())
        .filter((pl.col("training_period") == "same_year") & (pl.col("method") == "covariate_gbm"))
        .row(0, named=True)
    )

    assert row["n_intended"] == 3
    assert row["n_scored"] == 3
    assert row["n_failed"] == 0
    assert row["score_state"] == "missing_intended_predictions"
    assert row["station_clustered_mae"] is None


def test_paired_deltas_use_exact_keys_and_fixed_station_bootstrap() -> None:
    predictions = _score_prediction_fixture()

    deltas = paired_readiness_deltas(predictions, _model_config())
    row = deltas.filter(
        (pl.col("training_period") == "same_year") & (pl.col("method") == "covariate_gbm_idw2")
    ).row(0, named=True)
    expected = bootstrap_station_delta(np.asarray([-1.5, -2.5]), draws=9999, seed=20260828)

    assert row["comparison_method"] == "idw2"
    assert row["n_stations"] == 2
    assert row["median_station_mae_delta"] == expected[0]
    assert row["lower_2_5"] == expected[1]
    assert row["upper_97_5"] == expected[2]
    assert row["paired_state"] == "complete"


@pytest.mark.parametrize("mutation", ["missing_key", "extra_method", "duplicate_key"])
def test_paired_deltas_reject_unequal_method_domains_or_target_grids(mutation: str) -> None:
    predictions = _score_prediction_fixture()
    changed = (
        (pl.col("training_period") == "same_year")
        & (pl.col("method") == "covariate_gbm")
        & (pl.col("target_station") == "s0")
        & (pl.col("month") == date(2025, 1, 1))
    )
    if mutation == "missing_key":
        predictions = predictions.filter(~changed)
    elif mutation == "extra_method":
        predictions = pl.concat(
            [predictions, predictions.head(1).with_columns(pl.lit("extra").alias("method"))]
        )
    else:
        assert mutation == "duplicate_key"
        predictions = pl.concat([predictions, predictions.filter(changed)])

    with pytest.raises(RuntimeError, match=r"method domain|target keys|duplicate"):
        paired_readiness_deltas(predictions, _model_config())


def _gate_fixture(
    *, qualifying: tuple[str, ...] = ("covariate_gbm",)
) -> tuple[pl.DataFrame, pl.DataFrame]:
    score_rows: list[dict[str, object]] = []
    delta_rows: list[dict[str, object]] = []
    cells = [
        *(
            ("same_year", year, year, evaluation)
            for evaluation in _model_config().evaluations
            for year in (2024, 2025)
        ),
        *(("2024_to_2025", 2024, 2025, evaluation) for evaluation in _model_config().evaluations),
    ]
    for training_period, train_year, target_year, evaluation in cells:
        for method in _model_config().methods:
            score_rows.append(
                {
                    "evaluation": evaluation,
                    "training_period": training_period,
                    "train_year": train_year,
                    "target_year": target_year,
                    "method": method,
                    "n_intended": 24,
                    "n_scored": 24,
                    "n_failed": 0,
                    "n_stations_intended": 2,
                    "n_stations_scored": 2,
                    "station_clustered_mae": 1.0,
                    "station_clustered_rmse": 1.25,
                    "station_clustered_bias": 0.0,
                    "score_state": "complete",
                }
            )
            if method != "idw2":
                delta_rows.append(
                    {
                        "evaluation": evaluation,
                        "training_period": training_period,
                        "train_year": train_year,
                        "target_year": target_year,
                        "method": method,
                        "comparison_method": "idw2",
                        "n_stations": 2,
                        "median_station_mae_delta": -0.25 if method in qualifying else 0.25,
                        "lower_2_5": -0.5,
                        "upper_97_5": 0.1,
                        "paired_state": "complete",
                    }
                )
    return pl.DataFrame(score_rows), pl.DataFrame(delta_rows)


def _mutate_gate_evidence(
    scores: pl.DataFrame, deltas: pl.DataFrame, mutation: str
) -> tuple[pl.DataFrame, pl.DataFrame]:
    target_score = (
        (pl.col("training_period") == "same_year")
        & (pl.col("target_year") == 2024)
        & (pl.col("evaluation") == "buffer_20km")
        & (pl.col("method") == "covariate_gbm")
    )
    target_delta = (
        (pl.col("training_period") == "same_year")
        & (pl.col("target_year") == 2024)
        & (pl.col("evaluation") == "buffer_20km")
        & (pl.col("method") == "covariate_gbm")
    )
    if mutation == "zero_delta":
        deltas = deltas.with_columns(
            pl.when(target_delta)
            .then(pl.lit(0.0))
            .otherwise(pl.col("median_station_mae_delta"))
            .alias("median_station_mae_delta")
        )
    elif mutation == "positive_delta":
        deltas = deltas.with_columns(
            pl.when(target_delta)
            .then(pl.lit(0.01))
            .otherwise(pl.col("median_station_mae_delta"))
            .alias("median_station_mae_delta")
        )
    elif mutation == "missing_score":
        scores = scores.filter(~target_score)
    elif mutation == "failed_prediction":
        scores = scores.with_columns(
            pl.when(target_score).then(pl.lit(23)).otherwise(pl.col("n_scored")).alias("n_scored"),
            pl.when(target_score).then(pl.lit(1)).otherwise(pl.col("n_failed")).alias("n_failed"),
            pl.when(target_score)
            .then(pl.lit("incomplete_predictions"))
            .otherwise(pl.col("score_state"))
            .alias("score_state"),
        )
    elif mutation == "wrong_comparator":
        deltas = deltas.with_columns(
            pl.when(target_delta)
            .then(pl.lit("covariate_gbm_idw2"))
            .otherwise(pl.col("comparison_method"))
            .alias("comparison_method")
        )
    elif mutation == "wrong_station_denominator":
        deltas = deltas.with_columns(
            pl.when(target_delta)
            .then(pl.lit(1))
            .otherwise(pl.col("n_stations"))
            .alias("n_stations")
        )
    elif mutation == "fractional_score_denominator":
        scores = scores.with_columns(
            pl.when(target_score)
            .then(pl.lit(24.5))
            .otherwise(pl.col("n_intended"))
            .alias("n_intended")
        )
    elif mutation == "fractional_paired_denominator":
        deltas = deltas.with_columns(
            pl.when(target_delta)
            .then(pl.lit(2.5))
            .otherwise(pl.col("n_stations"))
            .alias("n_stations")
        )
    elif mutation == "impossible_station_denominator":
        required_cell = (
            (pl.col("training_period") == "same_year")
            & (pl.col("target_year") == 2024)
            & (pl.col("evaluation") == "buffer_20km")
            & pl.col("method").is_in(["idw2", "covariate_gbm"])
        )
        scores = scores.with_columns(
            pl.when(required_cell)
            .then(pl.lit(25))
            .otherwise(pl.col("n_stations_intended"))
            .alias("n_stations_intended"),
            pl.when(required_cell)
            .then(pl.lit(25))
            .otherwise(pl.col("n_stations_scored"))
            .alias("n_stations_scored"),
        )
        deltas = deltas.with_columns(
            pl.when(target_delta)
            .then(pl.lit(25))
            .otherwise(pl.col("n_stations"))
            .alias("n_stations")
        )
    elif mutation == "wrong_cluster_comparator":
        cluster_delta = (
            (pl.col("training_period") == "same_year")
            & (pl.col("target_year") == 2024)
            & (pl.col("evaluation") == "spatial_cluster")
            & (pl.col("method") == "covariate_gbm")
        )
        deltas = deltas.with_columns(
            pl.when(cluster_delta)
            .then(pl.lit("extra"))
            .otherwise(pl.col("comparison_method"))
            .alias("comparison_method")
        )
    elif mutation == "missing_cluster_cell":
        scores = scores.filter(
            ~(
                (pl.col("training_period") == "same_year")
                & (pl.col("target_year") == 2025)
                & (pl.col("evaluation") == "spatial_cluster")
                & (pl.col("method") == "covariate_gbm")
            )
        )
    elif mutation == "absent_year":
        scores = scores.filter(pl.col("target_year") != 2024)
        deltas = deltas.filter(pl.col("target_year") != 2024)
    else:
        assert mutation == "extra_method"
        scores = pl.concat([scores, scores.head(1).with_columns(pl.lit("extra").alias("method"))])
    return scores, deltas


def test_gate_requires_all_seven_improvement_cells_and_complete_cluster_scores() -> None:
    scores, deltas = _gate_fixture()

    verdict = decide_covariate_gate(scores, deltas, _model_config())

    assert verdict == {
        "state": "go",
        "qualifying_methods": ["covariate_gbm"],
        "required_improvement_cells": 7,
        "rule": (
            "complete predictions and median station MAE delta < 0 versus idw2 "
            "in 2024/2025 same-year 20/40 km and all 2024-to-2025 joint cells"
        ),
        "limitations": list(COVARIATE_READINESS_LIMITATIONS),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "zero_delta",
        "positive_delta",
        "missing_score",
        "failed_prediction",
        "wrong_comparator",
        "wrong_station_denominator",
        "fractional_score_denominator",
        "fractional_paired_denominator",
        "impossible_station_denominator",
        "wrong_cluster_comparator",
        "missing_cluster_cell",
        "absent_year",
        "extra_method",
    ],
)
def test_gate_fails_closed_for_each_required_evidence_mutation(mutation: str) -> None:
    scores, deltas = _mutate_gate_evidence(*_gate_fixture(), mutation)

    try:
        verdict = decide_covariate_gate(scores, deltas, _model_config())
    except RuntimeError:
        return
    assert verdict["state"] == "stop"
    assert verdict["qualifying_methods"] == []


def test_gate_is_go_for_a_second_qualifying_candidate_and_stop_for_none() -> None:
    second_scores, second_deltas = _gate_fixture(qualifying=("covariate_gbm_idw2",))
    stop_scores, stop_deltas = _gate_fixture(qualifying=())

    assert decide_covariate_gate(second_scores, second_deltas, _model_config())[
        "qualifying_methods"
    ] == ["covariate_gbm_idw2"]
    assert decide_covariate_gate(stop_scores, stop_deltas, _model_config())["state"] == "stop"


def test_gate_rejects_a_direct_config_year_mutation() -> None:
    scores, deltas = _gate_fixture()

    with pytest.raises(RuntimeError, match="configuration"):
        decide_covariate_gate(
            scores,
            deltas,
            replace(_model_config(), years=(2023, 2024)),
        )


def test_shipped_config_pins_the_reviewed_covariate_contract() -> None:
    config = load_spatial_covariate_readiness_config()

    assert config.years == (2024, 2025)
    assert config.baseline_generation_sha256 == BASELINE_GENERATION
    assert config.station_inventory_generation_sha256 == INVENTORY_GENERATION
    assert config.minimum_train_stations == 8
    assert config.methods == ("idw2", "covariate_gbm", "covariate_gbm_idw2")
    assert config.comparator == "idw2"
    assert config.idw_power == 2.0
    assert config.minimum_distance_km == 0.1
    assert config.model.n_estimators == 200
    assert config.model.learning_rate == 0.05
    assert config.model.num_leaves == 31
    assert config.model.min_child_samples == 10
    assert config.model.n_jobs == 1
    assert config.model.seed == 20260811
    assert config.evaluations == ("buffer_20km", "buffer_40km", "spatial_cluster")
    assert config.bootstrap_draws == 9999
    assert config.bootstrap_seed == 20260828


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (
            lambda raw: raw.__setitem__("schema_version", 2),
            "spatial_covariate_readiness.schema_version",
        ),
        (
            lambda raw: raw["analysis"].__setitem__("years", [2025, 2024]),
            "spatial_covariate_readiness.analysis.years",
        ),
        (
            lambda raw: raw["analysis"].__setitem__("baseline_generation_sha256", "not-a-sha"),
            "spatial_covariate_readiness.analysis.baseline_generation_sha256",
        ),
        (
            lambda raw: raw["methods"].__setitem__(
                "candidates", ["covariate_gbm_idw2", "covariate_gbm"]
            ),
            "spatial_covariate_readiness.methods.candidates",
        ),
        (
            lambda raw: raw["methods"].__setitem__("comparator", "nearest"),
            "spatial_covariate_readiness.methods.comparator",
        ),
        (
            lambda raw: raw["methods"].__setitem__("minimum_distance_km", 0.0),
            "spatial_covariate_readiness.methods.minimum_distance_km",
        ),
        (
            lambda raw: raw["validation"].__setitem__("bootstrap_draws", 0),
            "spatial_covariate_readiness.validation.bootstrap_draws",
        ),
        (
            lambda raw: raw["methods"]["model"].__setitem__("n_jobs", 2),
            "spatial_covariate_readiness.methods.model.n_jobs",
        ),
        (
            lambda raw: raw["methods"]["model"].__setitem__("seed", 20260812),
            "spatial_covariate_readiness.methods.model.seed",
        ),
        (
            lambda raw: raw["validation"].__setitem__(
                "evaluations", ["buffer_20km", "buffer_20km", "spatial_cluster"]
            ),
            "spatial_covariate_readiness.validation.evaluations",
        ),
        (
            lambda raw: raw["validation"].__setitem__(
                "evaluations", ["buffer_20km", "buffer_40km", "spatial_regions"]
            ),
            "spatial_covariate_readiness.validation.evaluations",
        ),
    ],
)
def test_config_rejects_any_drift_with_its_exact_path(mutate: Any, path: str) -> None:
    raw = copy.deepcopy(_config_payload())
    mutate(raw)

    with pytest.raises(ConfigError, match=path):
        load_spatial_covariate_readiness_config(raw)


def _baseline_directory(root: Path) -> Path:
    return root / "outputs" / "spatial_surface_baseline" / "generations" / BASELINE_GENERATION


def _write_baseline_fixture(
    root: Path,
    *,
    station_count: int = 59,
    omitted_panel_key: tuple[str, date] | None = None,
    withheld_key: tuple[str, date] = ("新營", date(2025, 5, 1)),
    extra_withheld: tuple[str, date] | None = None,
) -> list[Path]:
    directory = _baseline_directory(root)
    directory.mkdir(parents=True)
    station_names = [*(f"station-{index:02d}" for index in range(station_count - 1)), "新營"]
    months = [date(year, month, 1) for year in (2024, 2025) for month in range(1, 13)]
    stations = pl.DataFrame(
        {
            "station_name": station_names,
            "station_type_official": ["一般站"] * station_count,
            "lon": [120.0 + index * 0.01 for index in range(station_count)],
            "lat": [23.0 + index * 0.01 for index in range(station_count)],
        },
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["stations"],
    )
    panel_rows: list[dict[str, object]] = []
    for station_index, station_name in enumerate(station_names):
        for month in months:
            if (station_name, month) == omitted_panel_key:
                continue
            withheld = (station_name, month) == withheld_key or (
                station_name,
                month,
            ) == extra_withheld
            panel_rows.append(
                {
                    "station_name": station_name,
                    "station_type_official": "一般站",
                    "lon": 120.0 + station_index * 0.01,
                    "lat": 23.0 + station_index * 0.01,
                    "month": month,
                    "pollutant": "PM2.5",
                    "mean": None if withheld else float(10 + station_index + month.month),
                    "meets_threshold": not withheld,
                    "target_state": "withheld" if withheld else "observed",
                }
            )
    panel = pl.DataFrame(
        panel_rows,
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["panel"],
    )
    support = pl.DataFrame(
        {
            "station_name": station_names,
            "nearest_station": [
                station_names[(index + 1) % station_count] for index in range(station_count)
            ],
            "nearest_station_km": [1.0] * station_count,
            "stations_within_20km": [station_count - 1] * station_count,
            "stations_within_40km": [station_count - 1] * station_count,
            "x_m": [100.0 + index for index in range(station_count)],
            "y_m": [300.0 + index for index in range(station_count)],
        },
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["support"],
    )
    fold_rows: list[dict[str, object]] = []
    for evaluation in ("buffer_20km", "buffer_40km", "spatial_cluster"):
        for row in panel.iter_rows(named=True):
            station_name = str(row["station_name"])
            target_state = str(row["target_state"])
            fold_rows.append(
                {
                    "evaluation": evaluation,
                    "fold_id": f"{evaluation}:{station_name}",
                    "year": row["month"].year,
                    "month": row["month"],
                    "target_station": station_name,
                    "target_cluster": station_names.index(station_name) % 5,
                    "target_state": target_state,
                    "observed": row["mean"],
                    "train_stations": [name for name in station_names if name != station_name],
                    "n_train": station_count - 1,
                    "fold_state": (
                        "eligible" if target_state == "observed" else "unscored_target_withheld"
                    ),
                    "fold_reason": None if target_state == "observed" else "target_withheld",
                }
            )
    folds = pl.DataFrame(fold_rows, schema=SPATIAL_BASELINE_TABLE_SCHEMAS["folds"])
    frames = {"stations": stations, "panel": panel, "support": support, "folds": folds}
    for name, frame in frames.items():
        frame.write_parquet(directory / f"{name}.parquet")
    member_paths = [directory / f"{name}.parquet" for name in frames]
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "complete": True,
                "generation_sha256": BASELINE_GENERATION,
                "inventory_generation_sha256": INVENTORY_GENERATION,
                "members": {
                    path.name: {"bytes": path.stat().st_size, "sha256": _identity(path).sha256}
                    for path in member_paths
                },
            }
        ),
        encoding="utf-8",
    )
    return [manifest_path, *(directory / f"{name}.parquet" for name in frames)]


def _write_external_inputs(root: Path) -> list[Path]:
    paths: list[Path] = []
    for year in (2023, 2024, 2025):
        path = (
            root
            / "interim"
            / "era5"
            / "generations"
            / INVENTORY_GENERATION
            / f"year={year}"
            / "era5_station_hour.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"era5-{year}".encode())
        paths.append(path)
    for year in (2024, 2025):
        path = (
            root
            / "outputs"
            / "m8_satellite"
            / "generations"
            / INVENTORY_GENERATION
            / f"year={year}"
            / "panel.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"m8-{year}".encode())
        paths.append(path)
    return paths


def _identity(path: Path) -> InputFile:
    payload = path.read_bytes()
    return InputFile(path=path, bytes=len(payload), sha256=sha256(payload).hexdigest())


def _refresh_manifest_member_identity(root: Path, member: str) -> None:
    directory = _baseline_directory(root)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = _identity(directory / member)
    manifest["members"][member] = {"bytes": identity.bytes, "sha256": identity.sha256}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _write_frozen_fixture(
    root: Path,
    *,
    station_count: int = 59,
    omitted_panel_key: tuple[str, date] | None = None,
    withheld_key: tuple[str, date] = ("新營", date(2025, 5, 1)),
    extra_withheld: tuple[str, date] | None = None,
) -> tuple[CovariateReadinessConfig, list[Path]]:
    baseline = _write_baseline_fixture(
        root,
        station_count=station_count,
        omitted_panel_key=omitted_panel_key,
        withheld_key=withheld_key,
        extra_withheld=extra_withheld,
    )
    external = _write_external_inputs(root)
    return _config(), [*baseline, *external]


def test_frozen_inputs_return_sorted_baseline_tables_and_exact_identities(tmp_path: Path) -> None:
    config, expected_paths = _write_frozen_fixture(tmp_path)

    inputs = load_frozen_inputs(tmp_path, config)

    assert inputs.stations.height == 59
    assert inputs.stations["station_name"].to_list()[-1] == "新營"
    assert inputs.panel.height == 1416
    assert inputs.support.height == 59
    assert inputs.baseline_folds.height == 3 * 1416
    assert inputs.panel.filter(pl.col("target_state") == "observed").height == 1415
    assert inputs.panel.filter(pl.col("target_state") == "withheld").select(
        "station_name", "month", "mean"
    ).rows() == [("新營", date(2025, 5, 1), None)]
    assert inputs.input_files == tuple(_identity(path) for path in expected_paths)
    assert inputs.baseline_generation_sha256 == BASELINE_GENERATION
    assert inputs.station_inventory_generation_sha256 == INVENTORY_GENERATION


def test_frozen_inputs_reject_a_generation_directory_other_than_the_reviewed_one(
    tmp_path: Path,
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    directory = _baseline_directory(tmp_path)
    directory.rename(directory.with_name("c" * 64))

    with pytest.raises(RuntimeError, match="generation is missing"):
        load_frozen_inputs(tmp_path, config)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("complete", False, "complete"),
        ("generation_sha256", "c" * 64, "generation"),
    ],
)
def test_frozen_inputs_reject_an_incomplete_or_mismatched_manifest(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    manifest_path = _baseline_directory(tmp_path) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        load_frozen_inputs(tmp_path, config)


@pytest.mark.parametrize(
    "member", ["stations.parquet", "panel.parquet", "support.parquet", "folds.parquet"]
)
def test_frozen_inputs_reject_a_missing_baseline_member(tmp_path: Path, member: str) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    (_baseline_directory(tmp_path) / member).unlink()

    with pytest.raises(RuntimeError, match="missing"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_an_unexpected_baseline_table_schema(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    pl.read_parquet(panel_path).drop("target_state").write_parquet(panel_path)
    _refresh_manifest_member_identity(tmp_path, "panel.parquet")

    with pytest.raises(RuntimeError, match="panel schema"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_a_baseline_member_that_differs_from_its_manifest_identity(
    tmp_path: Path,
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path).with_columns(
        pl.when(pl.col("target_state") == "observed")
        .then(pl.col("mean") + 0.25)
        .otherwise(pl.col("mean"))
        .alias("mean")
    )
    panel.write_parquet(panel_path)

    with pytest.raises(RuntimeError, match="manifest member identity"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_duplicate_station_month_keys(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path)
    pl.concat([panel, panel.head(1)]).write_parquet(panel_path)
    _refresh_manifest_member_identity(tmp_path, "panel.parquet")

    with pytest.raises(RuntimeError, match="duplicate station-month"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_a_panel_station_missing_from_stations_or_support(
    tmp_path: Path,
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path).with_columns(
        pl.when(pl.col("station_name") == "station-01")
        .then(pl.lit("gamma"))
        .otherwise(pl.col("station_name"))
        .alias("station_name")
    )
    panel.write_parquet(panel_path)
    _refresh_manifest_member_identity(tmp_path, "panel.parquet")

    with pytest.raises(RuntimeError, match="station set"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_target_states_outside_observed_and_withheld(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path).with_columns(
        pl.when(pl.col("station_name") == "新營")
        .then(pl.lit("source_row_absent"))
        .otherwise(pl.col("target_state"))
        .alias("target_state")
    )
    panel.write_parquet(panel_path)
    _refresh_manifest_member_identity(tmp_path, "panel.parquet")

    with pytest.raises(RuntimeError, match="target states"):
        load_frozen_inputs(tmp_path, config)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("station_count", "station count"),
        ("panel_key_count", "panel key count"),
        ("withheld_count", "withheld count"),
    ],
)
def test_frozen_inputs_reject_wrong_reviewed_cohort_counts(
    tmp_path: Path, mutation: str, message: str
) -> None:
    if mutation == "station_count":
        config, _ = _write_frozen_fixture(tmp_path, station_count=58)
    elif mutation == "panel_key_count":
        config, _ = _write_frozen_fixture(
            tmp_path, omitted_panel_key=("station-00", date(2024, 1, 1))
        )
    else:
        assert mutation == "withheld_count"
        config, _ = _write_frozen_fixture(tmp_path, extra_withheld=("station-00", date(2024, 1, 1)))

    with pytest.raises(RuntimeError, match=message):
        load_frozen_inputs(tmp_path, config)


@pytest.mark.parametrize(
    "withheld_key",
    [("station-00", date(2025, 5, 1)), ("新營", date(2025, 4, 1))],
)
def test_frozen_inputs_reject_an_unreviewed_withheld_identity(
    tmp_path: Path, withheld_key: tuple[str, date]
) -> None:
    config, _ = _write_frozen_fixture(tmp_path, withheld_key=withheld_key)

    with pytest.raises(RuntimeError, match="withheld identity"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_target_state_counts_that_differ_from_folds(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    folds_path = _baseline_directory(tmp_path) / "folds.parquet"
    changed = (pl.col("target_station") == "station-00") & (pl.col("month") == date(2025, 1, 1))
    folds = pl.read_parquet(folds_path).with_columns(
        pl.when(changed)
        .then(pl.lit("withheld"))
        .otherwise(pl.col("target_state"))
        .alias("target_state"),
    )
    folds.write_parquet(folds_path)
    _refresh_manifest_member_identity(tmp_path, "folds.parquet")

    with pytest.raises(RuntimeError, match="target-state counts"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_a_station_inventory_generation_mismatch(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    manifest_path = _baseline_directory(tmp_path) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inventory_generation_sha256"] = "c" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="station inventory generation"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_a_file_changed_during_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    real_read_parquet = pl.read_parquet

    def read_then_change(path: str | Path, *args: Any, **kwargs: Any) -> pl.DataFrame:
        frame = real_read_parquet(path, *args, **kwargs)
        if Path(path) == panel_path:
            panel_path.write_bytes(panel_path.read_bytes() + b"changed")
        return frame

    monkeypatch.setattr(pl, "read_parquet", read_then_change)

    with pytest.raises(RuntimeError, match="changed while it was read"):
        load_frozen_inputs(tmp_path, config)


def _result_assembly_fixture(tmp_path: Path) -> tuple[FrozenInputs, dict[str, pl.DataFrame]]:
    covariates, support, baseline_folds = _model_fixture()
    stations = (
        covariates.select("station_name", "lon", "lat")
        .unique()
        .with_columns(pl.lit("一般站").alias("station_type_official"))
        .select(*SPATIAL_BASELINE_TABLE_SCHEMAS["stations"])
        .cast(pl.Schema(SPATIAL_BASELINE_TABLE_SCHEMAS["stations"]))
    )
    panel = (
        covariates.select(
            "station_name",
            pl.lit("一般站").alias("station_type_official"),
            "lon",
            "lat",
            "month",
            pl.lit("PM2.5").alias("pollutant"),
            pl.col("PM2.5").alias("mean"),
            (pl.col("target_state") == "observed").alias("meets_threshold"),
            "target_state",
        )
        .select(*SPATIAL_BASELINE_TABLE_SCHEMAS["panel"])
        .cast(pl.Schema(SPATIAL_BASELINE_TABLE_SCHEMAS["panel"]))
    )
    config = _config()
    folds = build_covariate_fold_ledger(covariates, support, baseline_folds, config)
    prediction_rows: list[dict[str, object]] = []
    offsets = {"idw2": 1.0, "covariate_gbm": 0.5, "covariate_gbm_idw2": 0.25}
    for fold in folds.iter_rows(named=True):
        eligible = fold["fold_state"] == "eligible"
        for method, offset in offsets.items():
            observed = fold["observed"]
            prediction_rows.append(
                {
                    **fold,
                    "method": method,
                    "predicted": float(observed) + offset
                    if eligible and observed is not None
                    else None,
                    "prediction_state": "scored" if eligible else str(fold["fold_state"]),
                    "failure_type": None,
                    "error": offset if eligible else None,
                }
            )
    predictions = pl.DataFrame(prediction_rows, schema=readiness._COVARIATE_PREDICTION_SCHEMA)
    scores = score_readiness_predictions(predictions, config)
    paired_deltas = paired_readiness_deltas(predictions, config)
    input_path = tmp_path / "inputs" / "source.bin"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"a")
    inputs = FrozenInputs(
        stations=stations,
        panel=panel,
        support=support,
        baseline_folds=baseline_folds,
        input_files=(InputFile(path=input_path, bytes=1, sha256=sha256(b"a").hexdigest()),),
        baseline_generation_sha256=BASELINE_GENERATION,
        station_inventory_generation_sha256=INVENTORY_GENERATION,
    )
    return inputs, {
        "covariates": covariates,
        "folds": folds,
        "predictions": predictions,
        "scores": scores,
        "paired_deltas": paired_deltas,
    }


def _run_result_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    generated_at: str = "2026-08-28T00:00:00+00:00",
    input_payload: bytes = b"a",
    minimum_distance_km: float = 0.1,
    mutate_table: tuple[str, str, object] | None = None,
    patch_git_state: bool = True,
) -> Any:
    inputs, frames = _result_assembly_fixture(tmp_path)
    input_path = inputs.input_files[0].path
    input_path.write_bytes(input_payload)
    inputs = replace(
        inputs,
        input_files=(
            InputFile(
                path=input_path,
                bytes=len(input_payload),
                sha256=sha256(input_payload).hexdigest(),
            ),
        ),
    )
    if mutate_table is not None:
        table, column, value = mutate_table
        frames[table] = frames[table].with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.lit(value))
            .otherwise(pl.col(column))
            .alias(column)
        )
    monkeypatch.setattr(readiness, "load_frozen_inputs", lambda *_args, **_kwargs: inputs)
    monkeypatch.setattr(
        readiness, "assemble_covariates", lambda *_args, **_kwargs: frames["covariates"]
    )
    monkeypatch.setattr(
        readiness, "build_covariate_fold_ledger", lambda *_args, **_kwargs: frames["folds"]
    )
    monkeypatch.setattr(
        readiness, "predict_readiness_methods", lambda *_args, **_kwargs: frames["predictions"]
    )
    monkeypatch.setattr(
        readiness, "score_readiness_predictions", lambda *_args, **_kwargs: frames["scores"]
    )
    monkeypatch.setattr(
        readiness, "paired_readiness_deltas", lambda *_args, **_kwargs: frames["paired_deltas"]
    )
    if patch_git_state:
        monkeypatch.setattr(readiness, "_exact_git_state", lambda: ("f" * 40, False))
    return readiness.run_spatial_covariate_readiness(
        data_root=tmp_path,
        config=replace(_config(), minimum_distance_km=minimum_distance_km),
        generated_at=generated_at,
    )


def test_result_assembly_normalizes_tables_and_builds_the_manifest_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)

    assert isinstance(result, readiness.CovariateReadinessResult)
    assert list(result.manifest) == [
        "schema_version",
        "analysis",
        "config",
        "inputs",
        "baseline_generation_sha256",
        "station_inventory_generation_sha256",
        "tables",
        "gate",
        "claim_boundary",
        "identity_scope",
        "git_sha",
        "git_dirty",
        "generated_at",
        "complete",
        "generation_sha256",
    ]
    assert result.manifest["complete"] is False
    assert result.manifest["baseline_generation_sha256"] == BASELINE_GENERATION
    assert result.manifest["station_inventory_generation_sha256"] == INVENTORY_GENERATION
    assert result.manifest["identity_scope"] == (
        "float-bearing output hashes and generation identity record one run; "
        "they are not cross-hardware identities"
    )
    assert result.summary["feeds_web"] is False
    assert result.summary["limitations"] == list(COVARIATE_READINESS_LIMITATIONS)
    for table, identity in result.manifest["tables"].items():
        assert identity["rows"] == getattr(result, table).height
        assert identity["schema"] == {
            column: str(dtype) for column, dtype in getattr(result, table).schema.items()
        }


def test_generation_identity_binds_input_bytes_and_normalized_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_result_fixture(tmp_path, monkeypatch)
    changed_input = _run_result_fixture(tmp_path, monkeypatch, input_payload=b"b")
    changed_config = _run_result_fixture(tmp_path, monkeypatch, minimum_distance_km=0.2)

    assert first.manifest["generation_sha256"] != changed_input.manifest["generation_sha256"]
    assert first.manifest["generation_sha256"] != changed_config.manifest["generation_sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        ("folds", "train_stations", ["different-station"]),
        ("predictions", "predicted", 999.0),
        ("scores", "station_clustered_mae", 999.0),
    ],
)
def test_generation_identity_binds_fold_membership_predictions_and_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: tuple[str, str, object],
) -> None:
    first = _run_result_fixture(tmp_path, monkeypatch)
    changed = _run_result_fixture(tmp_path, monkeypatch, mutate_table=mutation)

    assert first.manifest["generation_sha256"] != changed.manifest["generation_sha256"]


def test_generation_identity_excludes_generated_at_complete_and_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_result_fixture(tmp_path, monkeypatch)
    later = _run_result_fixture(
        tmp_path,
        monkeypatch,
        generated_at="2026-08-29T00:00:00+00:00",
    )
    mutated_envelope = {
        **first.manifest,
        "generated_at": "2099-01-01T00:00:00+00:00",
        "complete": True,
        "generation_sha256": "0" * 64,
    }

    assert first.manifest["generation_sha256"] == later.manifest["generation_sha256"]
    assert (
        readiness._canonical_hash(readiness._manifest_identity(mutated_envelope))
        == first.manifest["generation_sha256"]
    )


def test_writer_persists_exact_inventory_and_reuses_only_identical_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)
    output_root = tmp_path / "output"

    first = readiness.write_spatial_covariate_readiness_result(result, output_root=output_root)
    second = readiness.write_spatial_covariate_readiness_result(result, output_root=output_root)

    assert first == second
    generation = first["manifest"].parent
    assert {path.name for path in generation.iterdir()} == {
        "stations.parquet",
        "panel.parquet",
        "covariates.parquet",
        "folds.parquet",
        "predictions.parquet",
        "scores.parquet",
        "paired_deltas.parquet",
        "summary.json",
        "manifest.json",
    }
    first["scores"].write_bytes(first["scores"].read_bytes() + b"corrupt")
    with pytest.raises(RuntimeError, match="existing generation"):
        readiness.write_spatial_covariate_readiness_result(result, output_root=output_root)


def test_writer_reuses_a_concurrent_identical_winner_after_windows_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)
    output_root = tmp_path / "output"
    first = readiness.write_spatial_covariate_readiness_result(result, output_root=output_root)
    destination = first["manifest"].parent
    waiting_winner = tmp_path / "waiting-winner"
    destination.replace(waiting_winner)
    original_replace = Path.replace

    def concurrent_winner(self: Path, target: Path) -> Path:
        if self.name.startswith(".spatial-covariate-readiness.staging-"):
            original_replace(waiting_winner, target)
            raise PermissionError(13, "access denied", str(target), 5)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", concurrent_winner)

    reused = readiness.write_spatial_covariate_readiness_result(result, output_root=output_root)

    assert reused == first
    assert json.loads(reused["manifest"].read_text(encoding="utf-8"))["complete"] is True
    assert not list((output_root / "generations").glob(".spatial-covariate-readiness.staging-*"))


def test_writer_does_not_mask_permission_error_without_a_concurrent_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)
    original_replace = Path.replace

    def denied_promotion(self: Path, target: Path) -> Path:
        if self.name.startswith(".spatial-covariate-readiness.staging-"):
            raise PermissionError(13, "access denied", str(target), 5)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", denied_promotion)

    with pytest.raises(PermissionError, match="access denied"):
        readiness.write_spatial_covariate_readiness_result(
            result,
            output_root=tmp_path / "output",
        )

    assert not list(
        (tmp_path / "output" / "generations").glob(".spatial-covariate-readiness.staging-*")
    )


def _git_command_result(
    args: list[str],
    *,
    sha: str,
    status: str,
    revision_returncode: int = 0,
    status_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    if "rev-parse" in args:
        output = sha[:7] if "--short" in args else sha
        return subprocess.CompletedProcess(args, revision_returncode, stdout=output, stderr="")
    assert "status" in args
    return subprocess.CompletedProcess(args, status_returncode, stdout=status, stderr="")


def test_generation_identity_binds_exact_full_git_revision_and_dirty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha_a = "a" * 40
    sha_b = "b" * 40

    def run_with_git(*, sha: str, status: str) -> Any:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda args, **_kwargs: _git_command_result(
                args,
                sha=sha,
                status=status,
            ),
        )
        return _run_result_fixture(tmp_path, monkeypatch, patch_git_state=False)

    clean = run_with_git(sha=sha_a, status="")
    dirty = run_with_git(sha=sha_a, status=" M tracked.py\n")
    changed_revision = run_with_git(sha=sha_b, status="")

    assert clean.manifest["git_sha"] == sha_a
    assert clean.manifest["git_dirty"] is False
    assert dirty.manifest["git_sha"] == sha_a
    assert dirty.manifest["git_dirty"] is True
    assert changed_revision.manifest["git_sha"] == sha_b
    assert (
        len(
            {
                clean.manifest["generation_sha256"],
                dirty.manifest["generation_sha256"],
                changed_revision.manifest["generation_sha256"],
            }
        )
        == 3
    )


@pytest.mark.parametrize(
    ("sha", "status", "revision_returncode", "status_returncode"),
    [
        ("a" * 40, "", 1, 0),
        ("a" * 40, "", 0, 1),
        ("short", "", 0, 0),
        ("a" * 40, "not porcelain", 0, 0),
    ],
)
def test_result_assembly_fails_closed_for_unavailable_or_malformed_git_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sha: str,
    status: str,
    revision_returncode: int,
    status_returncode: int,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **_kwargs: _git_command_result(
            args,
            sha=sha,
            status=status,
            revision_returncode=revision_returncode,
            status_returncode=status_returncode,
        ),
    )

    with pytest.raises(RuntimeError, match="Git"):
        _run_result_fixture(tmp_path, monkeypatch, patch_git_state=False)


def test_result_assembly_fails_closed_when_git_cannot_be_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )

    with pytest.raises(RuntimeError, match="Git"):
        _run_result_fixture(tmp_path, monkeypatch, patch_git_state=False)


def test_writer_rejects_an_unexpected_existing_generation_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)
    written = readiness.write_spatial_covariate_readiness_result(
        result, output_root=tmp_path / "output"
    )
    unexpected = written["manifest"].parent / "unexpected.txt"
    unexpected.write_text("not reviewed", encoding="utf-8")

    with pytest.raises(RuntimeError, match="existing generation"):
        readiness.write_spatial_covariate_readiness_result(result, output_root=tmp_path / "output")

    assert unexpected.read_text(encoding="utf-8") == "not reviewed"


def test_atomic_writer_removes_only_its_exact_validated_staging_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)
    generations = tmp_path / "output" / "generations"
    generations.mkdir(parents=True)
    foreign_stage = generations / ".spatial-covariate-readiness.staging-foreign"
    foreign_stage.mkdir()
    foreign_sentinel = foreign_stage / "keep.txt"
    foreign_sentinel.write_text("keep", encoding="utf-8")
    root_sentinel = tmp_path / "output" / "keep.txt"
    root_sentinel.write_text("keep-root", encoding="utf-8")
    original_write_parquet = pl.DataFrame.write_parquet
    calls = 0

    def interrupt_after_first_member(
        self: pl.DataFrame, file: str | Path, *args: Any, **kwargs: Any
    ) -> None:
        nonlocal calls
        original_write_parquet(self, file, *args, **kwargs)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(pl.DataFrame, "write_parquet", interrupt_after_first_member)

    with pytest.raises(KeyboardInterrupt):
        readiness.write_spatial_covariate_readiness_result(result, output_root=tmp_path / "output")

    assert root_sentinel.read_text(encoding="utf-8") == "keep-root"
    assert foreign_sentinel.read_text(encoding="utf-8") == "keep"
    assert generations.is_dir()
    assert [
        path
        for path in generations.glob(".spatial-covariate-readiness.staging-*")
        if path != foreign_stage
    ] == []


def test_failed_atomic_promotion_preserves_the_interrupted_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)
    original_replace = Path.replace

    def promote_then_report_failure(self: Path, target: Path) -> Path:
        promoted = original_replace(self, target)
        if self.name.startswith(".spatial-covariate-readiness.staging-"):
            raise OSError("simulated post-rename failure")
        return promoted

    monkeypatch.setattr(Path, "replace", promote_then_report_failure)

    with pytest.raises(OSError, match="post-rename failure"):
        readiness.write_spatial_covariate_readiness_result(result, output_root=tmp_path / "output")

    generations = [
        path
        for path in (tmp_path / "output" / "generations").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert len(generations) == 1
    manifest_path = generations[0] / "manifest.json"
    interrupted = manifest_path.read_bytes()
    assert json.loads(interrupted.decode("utf-8"))["complete"] is False

    monkeypatch.setattr(Path, "replace", original_replace)
    with pytest.raises(RuntimeError, match="existing generation"):
        readiness.write_spatial_covariate_readiness_result(result, output_root=tmp_path / "output")
    assert manifest_path.read_bytes() == interrupted


def test_complete_true_appears_only_in_the_final_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)
    observed_before_promotion: list[bool] = []
    original_replace = Path.replace

    def observe_promotion(self: Path, target: Path) -> Path:
        if self.name.startswith(".spatial-covariate-readiness.staging-"):
            staged_manifest = json.loads((self / "manifest.json").read_text(encoding="utf-8"))
            observed_before_promotion.append(bool(staged_manifest["complete"]))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", observe_promotion)
    written = readiness.write_spatial_covariate_readiness_result(
        result, output_root=tmp_path / "output"
    )
    persisted = json.loads(written["manifest"].read_text(encoding="utf-8"))

    assert observed_before_promotion == [False]
    assert result.manifest["complete"] is False
    assert persisted["complete"] is True
    assert written["manifest"].parent.name == persisted["generation_sha256"]


def test_writer_rejects_a_symlinked_or_reparse_generations_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)
    generations = tmp_path / "output" / "generations"
    generations.mkdir(parents=True)
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == generations or real_is_symlink(path),
    )

    with pytest.raises(RuntimeError, match=r"reparse|linked"):
        readiness.write_spatial_covariate_readiness_result(result, output_root=tmp_path / "output")

    assert generations.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction contract")
def test_writer_rejects_an_ancestor_junction_before_creating_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_result_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked-ancestor"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("directory junctions are unavailable")

    with pytest.raises(RuntimeError, match=r"reparse|linked"):
        readiness.write_spatial_covariate_readiness_result(
            result,
            output_root=linked / "new" / "output",
        )

    assert linked.is_junction()
    assert list(outside.iterdir()) == []
