from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from copy import deepcopy
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import polars as pl
import pytest
from pykrige.ok import OrdinaryKriging

import twair.analysis.spatial_surface_baseline as spatial_surface_baseline
from twair.analysis.spatial_surface_baseline import (
    SpatialSurfaceBaselineConfig,
    SpatialSurfaceBaselineResult,
    SurfaceInputs,
    assign_spatial_clusters,
    bootstrap_station_delta,
    build_fold_ledger,
    build_station_support,
    decide_baseline_gate,
    evaluate_baselines,
    load_spatial_surface_baseline_config,
    load_surface_inputs,
    paired_method_deltas,
    predict_target,
    run_spatial_surface_baseline,
    score_predictions,
    write_spatial_surface_baseline_result,
)
from twair.config import ConfigError, load_conf


def _config(*, spatial_folds: int = 3) -> SpatialSurfaceBaselineConfig:
    return replace(
        load_spatial_surface_baseline_config(),
        spatial_folds=spatial_folds,
        bootstrap_draws=99,
    )


def synthetic_surface_inputs(
    stations: int,
    months: int,
    *,
    withheld: tuple[str, date] | None = None,
) -> SurfaceInputs:
    """Place s00 through sNN on a deterministic EPSG:3826-compatible mainland lattice."""
    station_names = [f"s{index:02d}" for index in range(stations)]
    station_frame = pl.DataFrame(
        {
            "station_name": station_names,
            "station_type_official": ["一般站"] * stations,
            "lon": [120.7 + 0.06 * index for index in range(stations)],
            "lat": [23.75 + 0.005 * (index % 3) for index in range(stations)],
        },
        schema={
            "station_name": pl.String,
            "station_type_official": pl.String,
            "lon": pl.Float64,
            "lat": pl.Float64,
        },
    )
    panel_rows: list[dict[str, object]] = []
    for month_index in range(months):
        month = date(2024, month_index + 1, 1)
        for station_index, station_name in enumerate(station_names):
            is_withheld = withheld == (station_name, month)
            panel_rows.append(
                {
                    "station_name": station_name,
                    "pollutant": "PM2.5",
                    "month": month,
                    "mean": None if is_withheld else float(10 + station_index + month_index),
                    "meets_threshold": not is_withheld,
                    "target_state": "withheld" if is_withheld else "observed",
                }
            )
    return SurfaceInputs(
        stations=station_frame,
        panel=pl.DataFrame(
            panel_rows,
            schema={
                "station_name": pl.String,
                "pollutant": pl.String,
                "month": pl.Date,
                "mean": pl.Float64,
                "meets_threshold": pl.Boolean,
                "target_state": pl.String,
            },
        ),
        input_files=(),
        inventory_generation_sha256="synthetic",
    )


def synthetic_fold_ledger() -> pl.DataFrame:
    """Return one eligible and one explicitly unscored production fold."""
    month = date(2024, 1, 1)
    return pl.DataFrame(
        {
            "evaluation": ["buffer_20km", "buffer_20km"],
            "fold_id": ["buffer_20km:s00", "buffer_20km:s09"],
            "year": [2024, 2024],
            "month": [month, month],
            "target_station": ["s00", "s09"],
            "target_cluster": [0, 1],
            "target_state": ["observed", "withheld"],
            "observed": [10.0, None],
            "train_stations": [
                ["s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08"],
                [],
            ],
            "n_train": [8, 0],
            "fold_state": ["eligible", "unscored_target_withheld"],
            "fold_reason": [None, "target_state=withheld"],
        },
        schema={
            "evaluation": pl.String,
            "fold_id": pl.String,
            "year": pl.Int64,
            "month": pl.Date,
            "target_station": pl.String,
            "target_cluster": pl.Int64,
            "target_state": pl.String,
            "observed": pl.Float64,
            "train_stations": pl.List(pl.String),
            "n_train": pl.Int64,
            "fold_state": pl.String,
            "fold_reason": pl.String,
        },
    )


def prediction_fixture(*, one_failure: bool = False) -> pl.DataFrame:
    """Return 3 stations × 4 months × 5 methods with canonical target keys."""
    rows: list[dict[str, object]] = []
    methods = _config().methods
    for station_index, station_name in enumerate(["s00", "s01", "s02"]):
        for month_index in range(4):
            month = date(2024, month_index + 1, 1)
            observed = float(20 + station_index + month_index)
            for method_index, method in enumerate(methods):
                failed = one_failure and method == "nearest" and station_index == month_index == 0
                predicted = None if failed else observed + float(method_index)
                rows.append(
                    {
                        "evaluation": "buffer_20km",
                        "fold_id": f"buffer_20km:{station_name}",
                        "year": 2024,
                        "month": month,
                        "target_station": station_name,
                        "target_cluster": station_index,
                        "target_state": "observed",
                        "observed": observed,
                        "train_stations": ["train-a", "train-b"],
                        "n_train": 2,
                        "fold_state": "eligible",
                        "fold_reason": None,
                        "method": method,
                        "predicted": predicted,
                        "kriging_sd": None,
                        "prediction_state": "estimator_failed" if failed else "scored",
                        "failure_type": "ValueError" if failed else None,
                        "error": None if predicted is None else predicted - observed,
                    }
                )
    return pl.DataFrame(rows).cast(
        {
            "evaluation": pl.String,
            "fold_id": pl.String,
            "year": pl.Int64,
            "month": pl.Date,
            "target_station": pl.String,
            "target_cluster": pl.Int64,
            "target_state": pl.String,
            "observed": pl.Float64,
            "train_stations": pl.List(pl.String),
            "n_train": pl.Int64,
            "fold_state": pl.String,
            "fold_reason": pl.String,
            "method": pl.String,
            "predicted": pl.Float64,
            "kriging_sd": pl.Float64,
            "prediction_state": pl.String,
            "failure_type": pl.String,
            "error": pl.Float64,
        }
    )


def gate_fixture(
    *, winner: str = "idw2", mutation: str | None = None
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return all evaluation-year cells with one optional gate-breaking change."""
    score_rows: list[dict[str, object]] = []
    delta_rows: list[dict[str, object]] = []
    for evaluation in ("buffer_20km", "buffer_40km", "spatial_cluster"):
        for year in (2024, 2025):
            for method in _config().methods:
                score_rows.append(
                    {
                        "evaluation": evaluation,
                        "year": year,
                        "method": method,
                        "n_intended": 12,
                        "n_scored": 12,
                        "n_failed": 0,
                        "n_stations_intended": 3,
                        "n_stations_scored": 3,
                        "station_clustered_mae": 1.0,
                        "station_clustered_rmse": 1.0,
                        "station_clustered_bias": 0.0,
                        "score_state": "complete",
                    }
                )
                if method != "station_mean":
                    delta_rows.append(
                        {
                            "evaluation": evaluation,
                            "year": year,
                            "method": method,
                            "comparison_method": "station_mean",
                            "n_stations": 3,
                            "median_station_mae_delta": -1.0 if method == winner else 0.0,
                            "lower_2_5": -1.0,
                            "upper_97_5": -1.0,
                            "paired_state": "complete",
                        }
                    )
    scores = pl.DataFrame(score_rows).cast(
        {
            "evaluation": pl.String,
            "year": pl.Int64,
            "method": pl.String,
            "n_intended": pl.Int64,
            "n_scored": pl.Int64,
            "n_failed": pl.Int64,
            "n_stations_intended": pl.Int64,
            "n_stations_scored": pl.Int64,
            "station_clustered_mae": pl.Float64,
            "station_clustered_rmse": pl.Float64,
            "station_clustered_bias": pl.Float64,
            "score_state": pl.String,
        }
    )
    deltas = pl.DataFrame(delta_rows).cast(
        {
            "evaluation": pl.String,
            "year": pl.Int64,
            "method": pl.String,
            "comparison_method": pl.String,
            "n_stations": pl.Int64,
            "median_station_mae_delta": pl.Float64,
            "lower_2_5": pl.Float64,
            "upper_97_5": pl.Float64,
            "paired_state": pl.String,
        }
    )
    if mutation == "missing_prediction":
        scores = scores.with_columns(
            pl.when(
                (pl.col("evaluation") == "buffer_20km")
                & (pl.col("year") == 2024)
                & (pl.col("method") == winner)
            )
            .then(pl.lit(11))
            .otherwise(pl.col("n_scored"))
            .alias("n_scored"),
            pl.when(
                (pl.col("evaluation") == "buffer_20km")
                & (pl.col("year") == 2024)
                & (pl.col("method") == winner)
            )
            .then(pl.lit(1))
            .otherwise(pl.col("n_failed"))
            .alias("n_failed"),
            pl.when(
                (pl.col("evaluation") == "buffer_20km")
                & (pl.col("year") == 2024)
                & (pl.col("method") == winner)
            )
            .then(pl.lit("incomplete_predictions"))
            .otherwise(pl.col("score_state"))
            .alias("score_state"),
        )
    elif mutation in {"loses_2024_40km", "loses_2025_20km"}:
        year, evaluation = (
            (2024, "buffer_40km") if mutation == "loses_2024_40km" else (2025, "buffer_20km")
        )
        deltas = deltas.with_columns(
            pl.when(
                (pl.col("evaluation") == evaluation)
                & (pl.col("year") == year)
                & (pl.col("method") == winner)
            )
            .then(pl.lit(0.0))
            .otherwise(pl.col("median_station_mae_delta"))
            .alias("median_station_mae_delta")
        )
    return scores, deltas


def distance_km(left: str, right: str, stations: pl.DataFrame) -> float:
    """Return projected fixture distance for a named pair."""
    points = gpd.GeoDataFrame(
        stations.to_pandas(),
        geometry=gpd.points_from_xy(stations["lon"].to_list(), stations["lat"].to_list()),
        crs="EPSG:4326",
    ).to_crs(epsg=3826)
    coordinates = {
        str(row.station_name): (float(row.geometry.x), float(row.geometry.y))
        for row in points.itertuples()
    }
    left_x, left_y = coordinates[left]
    right_x, right_y = coordinates[right]
    return math.hypot(left_x - right_x, left_y - right_y) / 1000


def write_surface_fixture(root: Path) -> Path:
    """Write qc/stations.parquet and monthly/monthly.parquet for 2024–2025.

    The inventory has general stations 一般甲/一般乙, background station 背景,
    industrial station 工業, and offshore general station 馬公. The accepted
    three stations each have 24 monthly PM2.5 rows; 背景/2025-12 has a present
    row with mean=null and meets_threshold=false.
    """
    stations_path = root / "outputs" / "qc" / "stations.parquet"
    monthly_path = root / "processed" / "monthly" / "monthly.parquet"
    stations_path.parent.mkdir(parents=True)
    monthly_path.parent.mkdir(parents=True)

    pl.DataFrame(
        {
            "station_name": ["背景", "一般甲", "一般乙", "工業", "馬公"],
            "station_type_official": ["背景站", "一般站", "一般站", "工業站", "一般站"],
            "lon": [121.3, 121.1, 121.2, 121.4, 119.6],
            "lat": [24.3, 24.1, 24.2, 24.4, 23.6],
        },
        schema={
            "station_name": pl.String,
            "station_type_official": pl.String,
            "lon": pl.Float64,
            "lat": pl.Float64,
        },
    ).write_parquet(stations_path)

    rows: list[dict[str, object]] = []
    for station_index, station in enumerate(["一般甲", "一般乙", "背景", "工業", "馬公"]):
        for year in (2024, 2025):
            for month in range(1, 13):
                withheld = station == "背景" and year == 2025 and month == 12
                rows.append(
                    {
                        "station_name": station,
                        "pollutant": "PM2.5",
                        "month": date(year, month, 1),
                        "mean": None if withheld else float(10 + station_index + month),
                        "meets_threshold": not withheld,
                    }
                )
    rows.append(
        {
            "station_name": "一般甲",
            "pollutant": "PM10",
            "month": date(2024, 1, 1),
            "mean": 999.0,
            "meets_threshold": True,
        }
    )
    pl.DataFrame(
        rows,
        schema={
            "station_name": pl.String,
            "pollutant": pl.String,
            "month": pl.Date,
            "mean": pl.Float64,
            "meets_threshold": pl.Boolean,
        },
    ).write_parquet(monthly_path)
    return root


def mutate_one_monthly_value(root: Path) -> None:
    """Change one finite fixture mean without changing its table shape."""
    monthly_path = _monthly_path(root)
    monthly = pl.read_parquet(monthly_path).with_columns(
        pl.when(
            (pl.col("station_name") == "一般甲")
            & (pl.col("pollutant") == "PM2.5")
            & (pl.col("month") == date(2024, 1, 1))
        )
        .then(pl.col("mean") + 0.25)
        .otherwise(pl.col("mean"))
        .alias("mean")
    )
    monthly.write_parquet(monthly_path)


def synthetic_baseline_result() -> SpatialSurfaceBaselineResult:
    """Return a complete self-consistent immutable-writer fixture."""
    with tempfile.TemporaryDirectory(prefix="twair-spatial-baseline-") as temporary:
        root = write_surface_fixture(Path(temporary))
        return run_spatial_surface_baseline(
            data_root=root,
            config=_config(spatial_folds=2),
            generated_at="2026-08-28T00:00:00+00:00",
        )


def corrupt_file(path: Path) -> None:
    """Flip the final byte of one artifact without changing its length."""
    payload = path.read_bytes()
    path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))


def _stations_path(root: Path) -> Path:
    return root / "outputs" / "qc" / "stations.parquet"


def _monthly_path(root: Path) -> Path:
    return root / "processed" / "monthly" / "monthly.parquet"


def test_shipped_config_pins_the_reviewed_baseline_contract() -> None:
    config = load_spatial_surface_baseline_config()

    assert config.years == (2024, 2025)
    assert config.pollutant == "PM2.5"
    assert config.station_types == ("一般站", "背景站")
    assert config.excluded_stations == ("馬公", "金門", "馬祖")
    assert config.buffer_radii_km == (20.0, 40.0)
    assert config.methods == (
        "station_mean",
        "nearest",
        "idw2",
        "kriging_spherical",
        "kriging_hole_effect",
    )
    assert config.min_train_stations == 8
    assert config.seed == 20260828


def test_config_rejects_missing_or_non_mapping_blocks() -> None:
    invalids: tuple[dict[str, object], ...] = ({}, {"analysis": []})
    for invalid in invalids:
        with pytest.raises(ConfigError):
            load_spatial_surface_baseline_config(invalid)
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_spatial_surface_baseline_config({"schema_version": 1, "analysis": []})


def test_primary_cohort_keeps_the_complete_key_grid_and_withheld_mean(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    inputs = load_surface_inputs(root, _config(spatial_folds=2))

    assert inputs.stations["station_name"].to_list() == ["背景", "一般甲", "一般乙"]
    assert inputs.panel.height == 3 * 24
    assert inputs.panel.filter(pl.col("target_state") == "withheld").height == 1
    assert inputs.panel.filter(pl.col("target_state") == "observed").height == 71
    assert inputs.panel.filter(pl.col("mean").is_null()).height == 1


def test_duplicate_station_names_are_rejected(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    stations = pl.read_parquet(_stations_path(root))
    pl.concat([stations, stations.head(1)]).write_parquet(_stations_path(root))

    with pytest.raises(RuntimeError, match="not unique"):
        load_surface_inputs(root, _config())


def test_partial_coordinates_are_rejected(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    stations = pl.read_parquet(_stations_path(root)).with_columns(
        pl.when(pl.col("station_name") == "一般甲")
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("lon"))
        .alias("lon")
    )
    stations.write_parquet(_stations_path(root))

    with pytest.raises(RuntimeError, match="both present or both null"):
        load_surface_inputs(root, _config())


def test_parseable_string_coordinates_return_as_canonical_float64(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    stations = pl.read_parquet(_stations_path(root)).with_columns(
        pl.col("lon").cast(pl.String),
        pl.col("lat").cast(pl.String),
    )
    stations.write_parquet(_stations_path(root))

    inputs = load_surface_inputs(root, _config())

    assert inputs.stations.schema["lon"] == pl.Float64
    assert inputs.stations.schema["lat"] == pl.Float64
    assert inputs.stations["lon"].to_list() == [121.3, 121.1, 121.2]
    assert inputs.stations["lat"].to_list() == [24.3, 24.1, 24.2]


@pytest.mark.parametrize("invalid_coordinate", [float("nan"), 150.0])
def test_non_finite_or_outside_taiwan_coordinates_are_rejected(
    tmp_path: Path, invalid_coordinate: float
) -> None:
    root = write_surface_fixture(tmp_path)
    stations = pl.read_parquet(_stations_path(root)).with_columns(
        pl.when(pl.col("station_name") == "一般甲")
        .then(pl.lit(invalid_coordinate))
        .otherwise(pl.col("lon"))
        .alias("lon")
    )
    stations.write_parquet(_stations_path(root))

    with pytest.raises(RuntimeError, match=r"not finite|outside Taiwan"):
        load_surface_inputs(root, _config())


def test_duplicate_station_month_keys_are_rejected(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    monthly = pl.read_parquet(_monthly_path(root))
    duplicate = monthly.filter(
        (pl.col("station_name") == "一般甲") & (pl.col("pollutant") == "PM2.5")
    ).head(1)
    pl.concat([monthly, duplicate]).write_parquet(_monthly_path(root))

    with pytest.raises(RuntimeError, match=r"duplicate PM2.5 station-month"):
        load_surface_inputs(root, _config())


def test_a_missing_source_calendar_key_remains_explicitly_absent(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    monthly = pl.read_parquet(_monthly_path(root)).filter(
        ~((pl.col("station_name") == "一般乙") & (pl.col("month") == date(2024, 6, 1)))
    )
    monthly.write_parquet(_monthly_path(root))

    inputs = load_surface_inputs(root, _config())
    missing = inputs.panel.filter(
        (pl.col("station_name") == "一般乙") & (pl.col("month") == date(2024, 6, 1))
    )
    assert missing.height == 1
    assert missing.item(0, "target_state") == "source_row_absent"


def test_non_pm25_rows_cannot_enter_the_panel(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    inputs = load_surface_inputs(root, _config())

    assert inputs.panel["pollutant"].unique().to_list() == ["PM2.5"]
    assert inputs.panel.filter(pl.col("mean") == 999.0).is_empty()


def test_source_file_changes_while_read_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_surface_fixture(tmp_path)
    monthly_path = _monthly_path(root)
    real_read_parquet = pl.read_parquet

    def read_then_change(path: str | Path, *args: Any, **kwargs: Any) -> pl.DataFrame:
        frame = real_read_parquet(path, *args, **kwargs)
        if Path(path) == monthly_path:
            monthly_path.write_bytes(monthly_path.read_bytes() + b"changed")
        return frame

    monkeypatch.setattr(pl, "read_parquet", read_then_change)

    with pytest.raises(RuntimeError, match="changed while it was read"):
        load_surface_inputs(root, _config())


def test_invalid_non_finite_monthly_means_are_not_observed(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    monthly = pl.read_parquet(_monthly_path(root)).with_columns(
        pl.when((pl.col("station_name") == "一般甲") & (pl.col("month") == date(2024, 2, 1)))
        .then(pl.lit(math.inf))
        .otherwise(pl.col("mean"))
        .alias("mean")
    )
    monthly.write_parquet(_monthly_path(root))

    inputs = load_surface_inputs(root, _config())
    state = inputs.panel.filter(
        (pl.col("station_name") == "一般甲") & (pl.col("month") == date(2024, 2, 1))
    ).item(0, "target_state")
    assert state == "invalid_non_finite"


def test_station_support_uses_projected_coordinates_and_counts_buffer_neighbors() -> None:
    inputs = synthetic_surface_inputs(stations=12, months=2)

    support = build_station_support(inputs.stations, _config(spatial_folds=3))
    first = support.filter(pl.col("station_name") == "s00").row(0, named=True)

    assert support["station_name"].to_list() == [f"s{index:02d}" for index in range(12)]
    assert first["x_m"] > 100_000
    assert first["y_m"] > 2_000_000
    assert first["nearest_station"] == "s01"
    assert 0 < first["nearest_station_km"] < 20
    assert first["stations_within_20km"] == 3
    assert first["stations_within_40km"] == 6


def test_spatial_clusters_are_deterministic_complete_and_canonically_ordered() -> None:
    inputs = synthetic_surface_inputs(stations=12, months=2)

    first = assign_spatial_clusters(inputs.stations, _config(spatial_folds=3))
    second = assign_spatial_clusters(inputs.stations, _config(spatial_folds=3))
    centroids = (
        first.group_by("spatial_cluster")
        .agg(pl.col("x_m").mean().alias("x_m"), pl.col("y_m").mean().alias("y_m"))
        .sort("spatial_cluster")
    )

    assert first.equals(second)
    assert first["station_name"].sort().to_list() == [f"s{index:02d}" for index in range(12)]
    assert first["station_name"].n_unique() == 12
    assert first["spatial_cluster"].unique().sort().to_list() == [0, 1, 2]
    assert list(zip(centroids["x_m"], centroids["y_m"], strict=True)) == sorted(
        zip(centroids["x_m"], centroids["y_m"], strict=True)
    )


def test_buffer_fold_excludes_target_and_every_station_inside_radius() -> None:
    inputs = synthetic_surface_inputs(stations=12, months=2)
    ledger = build_fold_ledger(inputs, _config(spatial_folds=3))
    fold = ledger.filter(
        (pl.col("evaluation") == "buffer_20km")
        & (pl.col("target_station") == "s00")
        & (pl.col("month") == date(2024, 1, 1))
    ).row(0, named=True)

    assert "s00" not in fold["train_stations"]
    assert all(distance_km("s00", name, inputs.stations) > 20 for name in fold["train_stations"])
    assert fold["target_state"] == "observed"


def test_withheld_target_stays_in_fold_ledger_but_is_never_scored() -> None:
    inputs = synthetic_surface_inputs(stations=12, months=2, withheld=("s00", date(2024, 1, 1)))
    ledger = build_fold_ledger(inputs, _config(spatial_folds=3))
    row = ledger.filter((pl.col("target_station") == "s00") & (pl.col("month") == date(2024, 1, 1)))

    assert row.height == 3
    assert set(row["fold_state"]) == {"unscored_target_withheld"}


def test_cluster_fold_keeps_a_station_in_one_cluster_across_months() -> None:
    inputs = synthetic_surface_inputs(stations=12, months=2)

    ledger = build_fold_ledger(inputs, _config(spatial_folds=3))
    cluster_rows = ledger.filter(pl.col("evaluation") == "spatial_cluster")

    assert (
        cluster_rows.group_by("target_station")
        .agg(pl.col("target_cluster").n_unique())
        .select(pl.col("target_cluster").max())
        .item()
        == 1
    )


def test_insufficient_training_stations_stays_as_an_unscored_fold() -> None:
    inputs = synthetic_surface_inputs(stations=12, months=2)

    ledger = build_fold_ledger(inputs, _config(spatial_folds=3))
    fold = ledger.filter(
        (pl.col("evaluation") == "buffer_40km")
        & (pl.col("target_station") == "s00")
        & (pl.col("month") == date(2024, 1, 1))
    ).row(0, named=True)

    assert fold["n_train"] < 8
    assert fold["fold_state"] == "unscored_insufficient_train"
    assert fold["fold_reason"] is not None


def test_simple_predictors_use_only_the_supplied_training_rows() -> None:
    train = pl.DataFrame(
        {
            "station_name": ["near", "far"],
            "lat": [23.0, 23.0],
            "lon": [120.1, 120.3],
            "mean": [10.0, 30.0],
        }
    )
    target = {"lat": 23.0, "lon": 120.0}

    assert predict_target(train, target, "station_mean", _config()).value == 20.0
    assert predict_target(train, target, "nearest", _config()).value == 10.0
    assert predict_target(train, target, "idw2", _config()).value == pytest.approx(12.0, abs=0.2)


def test_estimator_failure_remains_one_row_with_null_prediction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        OrdinaryKriging,
        "execute",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad fit")),
    )
    predictions = evaluate_baselines(
        synthetic_surface_inputs(12, 1), synthetic_fold_ledger(), _config()
    )
    failed = predictions.filter(pl.col("method") == "kriging_spherical")

    assert failed.height == synthetic_fold_ledger().height
    assert set(failed["prediction_state"]) == {"estimator_failed", "unscored_target_withheld"}
    scored_failure = failed.filter(pl.col("prediction_state") == "estimator_failed")
    assert scored_failure["predicted"].null_count() == scored_failure.height
    assert set(scored_failure["failure_type"]) == {"ValueError"}


def test_evaluation_emits_identical_five_method_rows_for_every_fold_key() -> None:
    predictions = evaluate_baselines(
        synthetic_surface_inputs(12, 1), synthetic_fold_ledger(), _config()
    )
    counts = predictions.group_by("evaluation", "fold_id", "target_station", "month").len()

    assert counts["len"].to_list() == [5, 5]
    assert predictions.group_by("evaluation", "fold_id", "target_station", "month").agg(
        pl.col("method").sort()
    )["method"].to_list() == [sorted(_config().methods), sorted(_config().methods)]


def test_ineligible_fold_emits_unscored_rows_without_calling_an_estimator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def estimator_was_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an unscored fold must not construct an estimator")

    monkeypatch.setattr(spatial_surface_baseline, "OrdinaryKriging", estimator_was_called)
    unscored_fold = synthetic_fold_ledger().filter(pl.col("fold_state") != "eligible")

    predictions = evaluate_baselines(synthetic_surface_inputs(12, 1), unscored_fold, _config())

    assert predictions.height == len(_config().methods)
    assert set(predictions["prediction_state"]) == {"unscored_target_withheld"}
    assert predictions["predicted"].null_count() == predictions.height


def test_duplicate_coordinates_fail_and_kriging_predictor_uses_fold_local_geographic_variograms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate_train = pl.DataFrame(
        {
            "station_name": ["same"],
            "lat": [23.0],
            "lon": [120.0],
            "mean": [10.0],
        }
    )
    duplicate = predict_target(
        duplicate_train,
        {"lat": 23.0, "lon": 120.0},
        "idw2",
        _config(),
    )
    calls: list[dict[str, object]] = []

    class RecordingKriging:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            calls.append(kwargs)

        def execute(self, *_args: object, **_kwargs: object) -> tuple[np.ndarray, np.ndarray]:
            return np.array([11.0]), np.array([4.0])

    monkeypatch.setattr(spatial_surface_baseline, "OrdinaryKriging", RecordingKriging)
    predictions = evaluate_baselines(
        synthetic_surface_inputs(12, 1), synthetic_fold_ledger().head(1), _config()
    )

    assert duplicate.state == "duplicate_coordinate"
    assert duplicate.value is None
    assert len(calls) == 2
    assert {call["variogram_model"] for call in calls} == {"spherical", "hole-effect"}
    assert {call["coordinates_type"] for call in calls} == {"geographic"}
    assert {call["nlags"] for call in calls} == {8}
    assert predictions.filter(pl.col("method").str.starts_with("kriging_"))[
        "prediction_state"
    ].to_list() == [
        "scored",
        "scored",
    ]


def test_predictors_reject_unrecognized_configured_methods_without_constructing_kriging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = deepcopy(load_conf("spatial_surface_baseline"))
    invalid["validation"]["methods"][-1] = "kriging_sphericl"
    invalid["gate"]["comparison_method"] = "kriging_sphericl"
    train = pl.DataFrame(
        {
            "station_name": ["near"],
            "lat": [23.0],
            "lon": [120.1],
            "mean": [10.0],
        }
    )
    malformed = replace(
        _config(),
        methods=("kriging_sphericl",),
        comparison_method="kriging_sphericl",
    )

    with pytest.raises(ConfigError, match="methods"):
        load_spatial_surface_baseline_config(invalid)
    monkeypatch.setattr(
        spatial_surface_baseline,
        "OrdinaryKriging",
        lambda *_args, **_kwargs: pytest.fail("unrecognized methods must fail before kriging"),
    )
    with pytest.raises(ValueError, match="not supported"):
        predict_target(train, {"lat": 23.0, "lon": 120.0}, "kriging_sphericl", malformed)


def test_evaluation_schema_stays_complete_for_success_failure_and_unscored_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_schema = {
        "evaluation": pl.String,
        "fold_id": pl.String,
        "year": pl.Int64,
        "month": pl.Date,
        "target_station": pl.String,
        "target_cluster": pl.Int64,
        "target_state": pl.String,
        "observed": pl.Float64,
        "train_stations": pl.List(pl.String),
        "n_train": pl.Int64,
        "fold_state": pl.String,
        "fold_reason": pl.String,
        "method": pl.String,
        "predicted": pl.Float64,
        "kriging_sd": pl.Float64,
        "prediction_state": pl.String,
        "failure_type": pl.String,
        "error": pl.Float64,
    }
    inputs = synthetic_surface_inputs(12, 1)
    eligible = synthetic_fold_ledger().head(1)
    unscored = synthetic_fold_ledger().filter(pl.col("fold_state") != "eligible")
    successful = evaluate_baselines(inputs, eligible, _config())

    monkeypatch.setattr(
        OrdinaryKriging,
        "execute",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad fit")),
    )
    failed = evaluate_baselines(inputs, eligible, _config())
    retained_unscored = evaluate_baselines(inputs, unscored, _config())

    assert successful.schema == expected_schema
    assert failed.schema == expected_schema
    assert retained_unscored.schema == expected_schema


def test_scores_report_intended_scored_and_failed_denominators() -> None:
    predictions = prediction_fixture(one_failure=True)

    scores = score_predictions(predictions, _config())
    row = scores.filter(pl.col("method") == "nearest").row(0, named=True)

    assert row["n_intended"] == 12
    assert row["n_scored"] == 11
    assert row["n_failed"] == 1
    assert row["n_stations_intended"] == 3
    assert row["n_stations_scored"] == 3
    assert row["score_state"] == "incomplete_predictions"
    assert row["station_clustered_mae"] == 1.0
    assert row["station_clustered_rmse"] == 1.0
    assert row["station_clustered_bias"] == 1.0


def test_scores_missing_method_row_uses_the_common_eligible_denominator() -> None:
    predictions = prediction_fixture().filter(
        ~(
            (pl.col("method") == "idw2")
            & (pl.col("target_station") == "s02")
            & (pl.col("month") == date(2024, 4, 1))
        )
    )

    scores = score_predictions(predictions, _config())
    row = scores.filter(pl.col("method") == "idw2").row(0, named=True)

    assert row["n_intended"] == 12
    assert row["n_scored"] == 11
    assert row["n_failed"] == 1
    assert row["n_stations_intended"] == 3
    assert row["n_stations_scored"] == 3
    assert row["score_state"] == "missing_intended_predictions"
    assert row["station_clustered_mae"] is None


def test_bootstrap_station_delta_is_byte_identical_for_a_fixed_seed() -> None:
    station_deltas = np.array([-2.0, -1.0, 0.0, 3.0])

    first = bootstrap_station_delta(station_deltas, draws=99, seed=20260828)
    second = bootstrap_station_delta(station_deltas, draws=99, seed=20260828)

    assert (
        np.asarray(first, dtype=np.float64).tobytes()
        == np.asarray(second, dtype=np.float64).tobytes()
    )


def test_paired_deltas_reject_unequal_method_and_baseline_target_keys() -> None:
    predictions = prediction_fixture().filter(
        ~((pl.col("method") == "idw2") & (pl.col("target_station") == "s02"))
    )

    with pytest.raises(RuntimeError, match="target keys"):
        paired_method_deltas(predictions, _config())


def test_gate_requires_one_complete_method_to_beat_station_mean_everywhere() -> None:
    scores, deltas = gate_fixture(winner="idw2")

    verdict = decide_baseline_gate(scores, deltas, _config())

    assert verdict["state"] == "go"
    assert verdict["qualifying_methods"] == ["idw2"]


@pytest.mark.parametrize("mutation", ["missing_prediction", "loses_2024_40km", "loses_2025_20km"])
def test_gate_fails_closed_on_incomplete_or_inconsistent_evidence(mutation: str) -> None:
    scores, deltas = gate_fixture(mutation=mutation)

    assert decide_baseline_gate(scores, deltas, _config())["state"] == "stop"


def test_gate_requires_a_complete_station_mean_score_for_every_primary_cell() -> None:
    scores, deltas = gate_fixture(winner="idw2")
    scores = scores.with_columns(
        pl.when(
            (pl.col("evaluation") == "buffer_20km")
            & (pl.col("year") == 2024)
            & (pl.col("method") == "station_mean")
        )
        .then(pl.lit(11))
        .otherwise(pl.col("n_scored"))
        .alias("n_scored"),
        pl.when(
            (pl.col("evaluation") == "buffer_20km")
            & (pl.col("year") == 2024)
            & (pl.col("method") == "station_mean")
        )
        .then(pl.lit(1))
        .otherwise(pl.col("n_failed"))
        .alias("n_failed"),
        pl.when(
            (pl.col("evaluation") == "buffer_20km")
            & (pl.col("year") == 2024)
            & (pl.col("method") == "station_mean")
        )
        .then(pl.lit("incomplete_predictions"))
        .otherwise(pl.col("score_state"))
        .alias("score_state"),
    )

    assert decide_baseline_gate(scores, deltas, _config())["state"] == "stop"


def test_gate_requires_paired_delta_station_count_to_match_the_score_denominator() -> None:
    scores, deltas = gate_fixture(winner="idw2")
    deltas = deltas.with_columns(
        pl.when(
            (pl.col("evaluation") == "buffer_20km")
            & (pl.col("year") == 2024)
            & (pl.col("method") == "idw2")
        )
        .then(pl.lit(1))
        .otherwise(pl.col("n_stations"))
        .alias("n_stations")
    )

    assert decide_baseline_gate(scores, deltas, _config())["state"] == "stop"


def test_spatial_cluster_score_cannot_substitute_for_a_missing_primary_buffer_cell() -> None:
    scores, deltas = gate_fixture(winner="idw2", mutation="missing_prediction")

    verdict = decide_baseline_gate(scores, deltas, _config())

    assert verdict["state"] == "stop"
    assert (
        scores.filter(
            (pl.col("evaluation") == "spatial_cluster")
            & (pl.col("method") == "idw2")
            & (pl.col("score_state") == "complete")
        ).height
        == 2
    )


def test_generation_identity_changes_when_any_bound_input_changes(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    first = run_spatial_surface_baseline(
        data_root=root,
        config=_config(spatial_folds=2),
        generated_at="2026-08-28T00:00:00+00:00",
    )
    mutate_one_monthly_value(root)
    second = run_spatial_surface_baseline(
        data_root=root,
        config=_config(spatial_folds=2),
        generated_at="2026-08-29T00:00:00+00:00",
    )

    assert first.manifest["generation_sha256"] != second.manifest["generation_sha256"]


def test_generation_identity_excludes_generated_at_but_binds_normalized_config(
    tmp_path: Path,
) -> None:
    root = write_surface_fixture(tmp_path)
    first = run_spatial_surface_baseline(
        data_root=root,
        config=_config(spatial_folds=2),
        generated_at="2026-08-28T00:00:00+00:00",
    )
    later = run_spatial_surface_baseline(
        data_root=root,
        config=_config(spatial_folds=2),
        generated_at="2026-08-29T00:00:00+00:00",
    )
    changed_config = run_spatial_surface_baseline(
        data_root=root,
        config=replace(_config(spatial_folds=2), minimum_distance_km=0.2),
        generated_at="2026-08-28T00:00:00+00:00",
    )

    assert first.manifest["generation_sha256"] == later.manifest["generation_sha256"]
    assert first.manifest["generation_sha256"] != changed_config.manifest["generation_sha256"]


def test_writer_reuses_an_identical_generation_and_refuses_collision(tmp_path: Path) -> None:
    result = synthetic_baseline_result()
    first = write_spatial_surface_baseline_result(result, output_root=tmp_path)
    second = write_spatial_surface_baseline_result(result, output_root=tmp_path)

    assert first == second
    corrupt_file(first["scores"])
    with pytest.raises(RuntimeError, match="existing generation"):
        write_spatial_surface_baseline_result(result, output_root=tmp_path)


def test_writer_reconstructs_summary_instead_of_trusting_prepared_summary(tmp_path: Path) -> None:
    result = synthetic_baseline_result()
    result.summary["feeds_web"] = True
    result.summary["gate"] = {"state": "go", "winning_method": "unreviewed"}

    with pytest.raises(RuntimeError, match="summary"):
        write_spatial_surface_baseline_result(result, output_root=tmp_path)

    assert not (tmp_path / "generations").exists()


def test_atomic_writer_removes_only_its_staging_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = synthetic_baseline_result()
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
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
        write_spatial_surface_baseline_result(result, output_root=tmp_path)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not list((tmp_path / "generations").glob(".spatial-surface-baseline.staging-*"))
    assert not [
        path
        for path in (tmp_path / "generations").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]


def test_failed_promotion_never_deletes_the_final_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = synthetic_baseline_result()
    original_replace = Path.replace

    def promote_then_report_failure(self: Path, target: Path) -> Path:
        promoted = original_replace(self, target)
        if self.name.startswith(".spatial-surface-baseline.staging-"):
            raise OSError("simulated post-rename failure")
        return promoted

    monkeypatch.setattr(Path, "replace", promote_then_report_failure)

    with pytest.raises(OSError, match="post-rename failure"):
        write_spatial_surface_baseline_result(result, output_root=tmp_path)

    generations = [
        path
        for path in (tmp_path / "generations").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert len(generations) == 1
    manifest_path = generations[0] / "manifest.json"
    interrupted_payload = manifest_path.read_bytes()
    assert json.loads(interrupted_payload.decode("utf-8"))["complete"] is False
    assert not list((tmp_path / "generations").glob(".spatial-surface-baseline.staging-*"))

    monkeypatch.setattr(Path, "replace", original_replace)
    with pytest.raises(RuntimeError, match="existing generation"):
        write_spatial_surface_baseline_result(result, output_root=tmp_path)

    assert manifest_path.read_bytes() == interrupted_payload


def test_staging_manifest_is_incomplete_immediately_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = Path.replace
    observed: list[bool] = []

    def observe_then_interrupt(self: Path, target: Path) -> Path:
        if self.name.startswith(".spatial-surface-baseline.staging-"):
            manifest = json.loads((self / "manifest.json").read_text(encoding="utf-8"))
            observed.append(bool(manifest["complete"]))
            raise KeyboardInterrupt
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", observe_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        write_spatial_surface_baseline_result(synthetic_baseline_result(), output_root=tmp_path)

    assert observed == [False]
    assert not list((tmp_path / "generations").glob(".spatial-surface-baseline.staging-*"))


def test_writer_rejects_an_unexpected_existing_generation_member(tmp_path: Path) -> None:
    result = synthetic_baseline_result()
    written = write_spatial_surface_baseline_result(result, output_root=tmp_path)
    unexpected = written["manifest"].parent / "unexpected.txt"
    unexpected.write_text("not reviewed", encoding="utf-8")

    with pytest.raises(RuntimeError, match="existing generation"):
        write_spatial_surface_baseline_result(result, output_root=tmp_path)

    assert unexpected.read_text(encoding="utf-8") == "not reviewed"


def test_writer_rejects_a_symlinked_existing_generation_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = synthetic_baseline_result()
    written = write_spatial_surface_baseline_result(result, output_root=tmp_path)
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == written["scores"] or real_is_symlink(path),
    )

    with pytest.raises(RuntimeError, match="existing generation"):
        write_spatial_surface_baseline_result(result, output_root=tmp_path)

    assert written["scores"].is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse-point contract")
def test_writer_rejects_a_windows_reparse_point_generations_destination(tmp_path: Path) -> None:
    outside = tmp_path / "outside-generations"
    outside.mkdir()
    linked = tmp_path / "generations"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("directory junctions are unavailable")

    with pytest.raises(RuntimeError, match=r"reparse|linked"):
        write_spatial_surface_baseline_result(synthetic_baseline_result(), output_root=tmp_path)

    assert linked.is_junction()
    assert outside.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse-point contract")
def test_writer_validates_junction_ancestor_before_creating_output_root(tmp_path: Path) -> None:
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
        write_spatial_surface_baseline_result(
            synthetic_baseline_result(),
            output_root=linked / "new" / "output",
        )

    assert linked.is_junction()
    assert list(outside.iterdir()) == []


def test_complete_true_appears_only_in_the_final_immutable_manifest(tmp_path: Path) -> None:
    result = synthetic_baseline_result()
    written = write_spatial_surface_baseline_result(result, output_root=tmp_path)
    persisted = json.loads(written["manifest"].read_text(encoding="utf-8"))

    assert result.manifest["complete"] is False
    assert persisted["complete"] is True
    assert written["manifest"].parent.name == persisted["generation_sha256"]
    assert {path.name for path in written["manifest"].parent.iterdir()} == {
        "stations.parquet",
        "panel.parquet",
        "support.parquet",
        "folds.parquet",
        "predictions.parquet",
        "scores.parquet",
        "paired_deltas.parquet",
        "summary.json",
        "manifest.json",
    }
