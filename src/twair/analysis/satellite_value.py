"""Test held-out predictive value in the immutable 2025 satellite panel.

The target and satellite columns retain their original physical meanings. The
models are a feasibility diagnostic on common observed rows, not a calibration
product, a concentration fusion field, or evidence about causes or sources.
"""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import numpy as np
import polars as pl

from twair.analysis.era5_robustness import assign_station_folds
from twair.analysis.era5_value import InputFile, ModelConfig
from twair.analysis.satellite import SatelliteAssociationResult, run_satellite_association
from twair.config import ConfigError, load_conf
from twair.ingest.station_inventory import (
    station_inventory_generation,
    validate_generation_sha256,
)
from twair.models.evaluate import evaluate_predictions
from twair.paths import data_root as configured_data_root
from twair.paths import outputs_dir
from twair.provenance import git_state
from twair.scalars import as_float, as_int

__all__ = [
    "SATELLITE_FEATURE_SETS",
    "SatelliteValueConfig",
    "SatelliteValueResult",
    "SatelliteValueRows",
    "evaluate_satellite_value",
    "load_satellite_value_config",
    "prepare_satellite_value_rows",
    "run_satellite_value",
    "satellite_metric_deltas",
    "summarise_satellite_deltas",
    "write_satellite_value_result",
]


SATELLITE_SOURCE_COLUMNS: dict[str, str] = {
    "maiac_aod": "maiac_aod",
    "s5p_no2": "s5p_no2",
    "s5p_so2": "s5p_so2",
}

SATELLITE_FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "baseline": ("month_sin", "month_cos", "lon", "lat"),
    "baseline_aod": ("month_sin", "month_cos", "lon", "lat", "maiac_aod"),
    "baseline_no2": ("month_sin", "month_cos", "lon", "lat", "s5p_no2"),
    "baseline_so2": ("month_sin", "month_cos", "lon", "lat", "s5p_so2"),
    "all_satellite": (
        "month_sin",
        "month_cos",
        "lon",
        "lat",
        "maiac_aod",
        "s5p_no2",
        "s5p_so2",
    ),
}

_COMPARISONS: tuple[tuple[str, str], ...] = (
    ("baseline_aod", "baseline"),
    ("baseline_no2", "baseline"),
    ("baseline_so2", "baseline"),
    ("all_satellite", "baseline"),
)

_PANEL_COLUMNS: tuple[str, ...] = (
    "source",
    "station_name",
    "month",
    "satellite_value",
    "ground_value",
    "satellite_observed",
    "ground_row_present",
    "ground_meets_threshold",
    "ground_observed",
    "ground_withheld",
    "pair_observed",
)

_GROUND_STATE_COLUMNS: tuple[str, ...] = (
    "ground_value",
    "ground_row_present",
    "ground_meets_threshold",
    "ground_observed",
    "ground_withheld",
)

_INVENTORY_COLUMNS: tuple[str, ...] = (
    "station_name",
    "airzone_official",
    "lon",
    "lat",
    "geo_source",
    "geo_source_record_namespace",
    "geo_source_record_id",
)


@dataclass(frozen=True, slots=True)
class SatelliteValueConfig:
    year: int
    quarter_folds: int
    station_folds: int
    model: ModelConfig


@dataclass(frozen=True, slots=True)
class SatelliteValueRows:
    values: pl.DataFrame
    coverage: pl.DataFrame
    station_folds: pl.DataFrame


@dataclass(frozen=True, slots=True)
class SatelliteValueResult:
    scores: pl.DataFrame
    deltas: pl.DataFrame
    coverage: pl.DataFrame
    station_folds: pl.DataFrame
    summary: dict[str, Any]
    manifest: dict[str, Any]


def _mapping(value: object, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def _positive_int(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{path} must be a positive integer")
    return value


def _positive_float(value: object, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a positive finite number")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ConfigError(f"{path} must be a positive finite number")
    return converted


def load_satellite_value_config(
    config: dict[str, Any] | None = None,
) -> SatelliteValueConfig:
    raw = config if config is not None else load_conf("satellite_value")
    group = _mapping(raw.get("analysis"), path="satellite_value.analysis")
    year = _positive_int(group.get("year"), path="satellite_value.analysis.year")
    quarter_folds = _positive_int(
        group.get("quarter_folds"), path="satellite_value.analysis.quarter_folds"
    )
    if quarter_folds != 4:
        raise ConfigError("satellite_value.analysis.quarter_folds must be four")
    station_folds = _positive_int(
        group.get("station_folds"), path="satellite_value.analysis.station_folds"
    )
    if station_folds < 2:
        raise ConfigError("satellite_value.analysis.station_folds must be at least two")
    raw_model = _mapping(group.get("model"), path="satellite_value.analysis.model")
    model = ModelConfig(
        n_estimators=_positive_int(
            raw_model.get("n_estimators"),
            path="satellite_value.analysis.model.n_estimators",
        ),
        learning_rate=_positive_float(
            raw_model.get("learning_rate"),
            path="satellite_value.analysis.model.learning_rate",
        ),
        num_leaves=_positive_int(
            raw_model.get("num_leaves"),
            path="satellite_value.analysis.model.num_leaves",
        ),
        min_child_samples=_positive_int(
            raw_model.get("min_child_samples"),
            path="satellite_value.analysis.model.min_child_samples",
        ),
        n_jobs=_positive_int(raw_model.get("n_jobs"), path="satellite_value.analysis.model.n_jobs"),
        seed=_positive_int(raw_model.get("seed"), path="satellite_value.analysis.model.seed"),
    )
    if model.n_jobs != 1:
        raise ConfigError("satellite_value analysis must use exactly one model job")
    return SatelliteValueConfig(
        year=year,
        quarter_folds=quarter_folds,
        station_folds=station_folds,
        model=model,
    )


def _require_columns(frame: pl.DataFrame, required: Iterable[str], *, label: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise RuntimeError(f"{label} is missing required column(s): {missing}")


def _bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{label} must be Boolean and non-null")
    return value


def _optional_finite(value: object, *, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{label} must be floating point or null")
    converted = float(value)
    if not math.isfinite(converted):
        raise RuntimeError(f"{label} must be finite or null")
    return converted


def _validated_panel_rows(panel: pl.DataFrame, *, year: int) -> list[dict[str, object]]:
    _require_columns(panel, _PANEL_COLUMNS, label="M8 satellite association panel")
    if panel.schema["month"] != pl.Date:
        raise RuntimeError("M8 satellite association month must use Date values")
    if panel.is_empty():
        raise RuntimeError("M8 satellite association panel is empty")
    if panel.filter(pl.col("month").dt.year() != year).height:
        raise RuntimeError("M8 satellite association rows must match the selected year")

    rows: list[dict[str, object]] = []
    for key, group in panel.group_by("station_name", "month", maintain_order=True):
        station_name, month = key
        if not isinstance(station_name, str) or not station_name.strip():
            raise RuntimeError("M8 satellite station names must be non-empty")
        if not isinstance(month, date):
            raise RuntimeError("M8 satellite month must use Date values")
        sources = group["source"].to_list()
        if group.height != len(SATELLITE_SOURCE_COLUMNS) or set(sources) != set(
            SATELLITE_SOURCE_COLUMNS
        ):
            raise RuntimeError(
                f"M8 satellite station-month lacks a complete source key: {station_name}/{month}"
            )
        if len(sources) != len(set(sources)):
            raise RuntimeError("M8 satellite station-month has a duplicate source key")

        states: list[tuple[float | None, bool, bool | None, bool, bool]] = []
        satellite_values: dict[str, float | None] = {}
        for item in group.iter_rows(named=True):
            source = str(item["source"])
            satellite = _optional_finite(item["satellite_value"], label=f"{source} satellite value")
            observed = _bool(item["satellite_observed"], label=f"{source} satellite observed flag")
            if observed != (satellite is not None):
                raise RuntimeError("satellite observed flags disagree with satellite nulls")
            satellite_values[SATELLITE_SOURCE_COLUMNS[source]] = satellite
            ground = _optional_finite(item["ground_value"], label="ground PM2.5 value")
            present = _bool(item["ground_row_present"], label="ground row-present flag")
            observed_ground = _bool(item["ground_observed"], label="ground observed flag")
            withheld = _bool(item["ground_withheld"], label="ground withheld flag")
            threshold = item["ground_meets_threshold"]
            if threshold is not None and not isinstance(threshold, bool):
                raise RuntimeError("ground threshold flag must be Boolean or null")
            if not present:
                if ground is not None or threshold is not None or observed_ground or withheld:
                    raise RuntimeError("ground absence flags disagree with the ground value")
            elif ground is None:
                if threshold is not False or observed_ground or not withheld:
                    raise RuntimeError("withheld ground flags disagree with the ground value")
            elif threshold is not True or not observed_ground or withheld:
                raise RuntimeError("observed ground flags disagree with the ground value")
            pair_observed = _bool(item["pair_observed"], label="pair observed flag")
            if pair_observed != (observed and observed_ground):
                raise RuntimeError("pair observed flags disagree with source and ground states")
            states.append((ground, present, threshold, observed_ground, withheld))
        if len(set(states)) != 1:
            raise RuntimeError(
                f"ground state differs across source rows for {station_name}/{month}"
            )
        ground, present, threshold, observed_ground, withheld = states[0]
        rows.append(
            {
                "station_name": station_name,
                "month": month,
                "ground_value": ground,
                "ground_row_present": present,
                "ground_meets_threshold": threshold,
                "ground_observed": observed_ground,
                "ground_withheld": withheld,
                **satellite_values,
            }
        )
    return rows


def _validated_inventory(inventory: pl.DataFrame) -> pl.DataFrame:
    _require_columns(inventory, _INVENTORY_COLUMNS, label="reviewed station inventory")
    selected = inventory.select(*_INVENTORY_COLUMNS)
    if selected["station_name"].n_unique() != selected.height:
        raise RuntimeError("reviewed station inventory has duplicate station names")
    if selected.filter(
        pl.col("station_name").is_null()
        | (pl.col("station_name").cast(pl.String, strict=False).str.strip_chars() == "")
    ).height:
        raise RuntimeError("reviewed station inventory has a missing station name")
    return selected.with_columns(
        pl.col("station_name").cast(pl.String),
        pl.col("airzone_official").cast(pl.String, strict=False),
        pl.col("lon").cast(pl.Float64, strict=False),
        pl.col("lat").cast(pl.Float64, strict=False),
        pl.col("geo_source").cast(pl.String, strict=False),
        pl.col("geo_source_record_namespace").cast(pl.String, strict=False),
        pl.col("geo_source_record_id").cast(pl.String, strict=False),
    )


def _coordinate_valid() -> pl.Expr:
    return (
        pl.when(pl.col("lon").is_null() | pl.col("lat").is_null())
        .then(False)
        .otherwise(pl.col("lon").is_finite() & pl.col("lat").is_finite())
    )


def prepare_satellite_value_rows(
    panel: pl.DataFrame,
    inventory: pl.DataFrame,
    *,
    config: SatelliteValueConfig,
) -> SatelliteValueRows:
    pivoted = pl.DataFrame(
        _validated_panel_rows(panel, year=config.year),
        schema_overrides={"month": pl.Date},
    ).sort("station_name", "month")
    reviewed = _validated_inventory(inventory)
    joined = pivoted.join(reviewed, on="station_name", how="left").with_columns(
        _coordinate_valid().alias("_coordinate_valid")
    )
    joined = joined.with_columns(
        (
            pl.col("ground_observed")
            & pl.col("maiac_aod").is_not_null()
            & pl.col("s5p_no2").is_not_null()
            & pl.col("s5p_so2").is_not_null()
            & pl.col("_coordinate_valid")
        ).alias("_common_complete")
    )
    coverage = pl.DataFrame(
        {
            "station_month_rows": [joined.height],
            "maiac_null_rows": [joined.filter(pl.col("maiac_aod").is_null()).height],
            "s5p_no2_null_rows": [joined.filter(pl.col("s5p_no2").is_null()).height],
            "s5p_so2_null_rows": [joined.filter(pl.col("s5p_so2").is_null()).height],
            "ground_absent_rows": [joined.filter(~pl.col("ground_row_present")).height],
            "ground_withheld_rows": [joined.filter(pl.col("ground_withheld")).height],
            "coordinate_missing_rows": [joined.filter(~pl.col("_coordinate_valid")).height],
            "common_complete_rows": [joined.filter(pl.col("_common_complete")).height],
        }
    )
    complete = joined.filter(pl.col("_common_complete")).with_columns(
        pl.col("ground_value").alias("PM2.5"),
        (((pl.col("month").dt.month() - 1) * (2.0 * math.pi / 12.0)).sin()).alias("month_sin"),
        (((pl.col("month").dt.month() - 1) * (2.0 * math.pi / 12.0)).cos()).alias("month_cos"),
        ((pl.col("month").dt.month() - 1) // 3).cast(pl.Int64).alias("quarter_fold"),
    )
    required_values = {
        "station_name",
        "month",
        "PM2.5",
        "quarter_fold",
        *(name for features in SATELLITE_FEATURE_SETS.values() for name in features),
    }
    if complete.is_empty():
        raise RuntimeError("satellite value analysis has no common complete rows")
    if complete.select(*sorted(required_values)).null_count().sum_horizontal().item():
        raise RuntimeError("satellite common rows contain an unexpected null")
    if complete["station_name"].n_unique() < 2:
        raise RuntimeError("satellite value analysis needs at least two complete stations")
    observed_quarters = sorted(complete["quarter_fold"].unique().to_list())
    if observed_quarters != list(range(config.quarter_folds)):
        raise RuntimeError("satellite value analysis is missing a calendar-quarter fold")

    station_names = complete["station_name"].unique().sort().to_list()
    membership_source = reviewed.filter(pl.col("station_name").is_in(station_names))
    provenance_columns = (
        "geo_source",
        "geo_source_record_namespace",
        "geo_source_record_id",
    )
    missing_provenance = membership_source.filter(
        pl.any_horizontal(
            *(
                pl.col(column).is_null()
                | (pl.col(column).cast(pl.String, strict=False).str.strip_chars() == "")
                for column in provenance_columns
            )
        )
    )
    if not missing_provenance.is_empty():
        raise RuntimeError("a complete station is missing reviewed geography provenance")
    effective_folds = min(config.station_folds, len(station_names))
    if effective_folds < 2:
        raise RuntimeError("satellite value analysis needs at least two station folds")
    assigned = assign_station_folds(membership_source, fold_count=effective_folds)
    membership = assigned.join(
        membership_source.select(
            "station_name",
            "lon",
            "lat",
            "geo_source",
            "geo_source_record_namespace",
            "geo_source_record_id",
        ),
        on="station_name",
        how="left",
    ).sort("station_name")
    return SatelliteValueRows(
        values=complete.select(
            "station_name",
            "month",
            "quarter_fold",
            "PM2.5",
            "month_sin",
            "month_cos",
            "lon",
            "lat",
            "maiac_aod",
            "s5p_no2",
            "s5p_so2",
        ).sort("station_name", "month"),
        coverage=coverage,
        station_folds=membership,
    )


def _fit_predict(
    train: pl.DataFrame,
    test: pl.DataFrame,
    features: tuple[str, ...],
    model: ModelConfig,
) -> tuple[np.ndarray, np.ndarray]:
    from twair.analysis.era5_value import _fit_predict as fit_predict

    return fit_predict(train, test, features, model)


def _score_fold(
    train: pl.DataFrame,
    test: pl.DataFrame,
    *,
    evaluation: str,
    fold: str,
    quarter_fold: int | None,
    station_fold: int | None,
    model: ModelConfig,
) -> list[dict[str, object]]:
    if train.is_empty() or test.height < 2:
        raise RuntimeError(
            f"satellite value fold {evaluation}/{fold} has too few train or test rows"
        )
    key_columns = ("station_name", "month")
    train_keys = set(train.select(*key_columns).iter_rows())
    test_keys = set(test.select(*key_columns).iter_rows())
    if train_keys & test_keys:
        raise RuntimeError(f"satellite value fold {evaluation}/{fold} leaked a test key")
    expected_truth = test["PM2.5"].to_numpy()
    rows: list[dict[str, object]] = []
    for feature_set, features in SATELLITE_FEATURE_SETS.items():
        started = perf_counter()
        truth, prediction = _fit_predict(train, test, features, model)
        elapsed = perf_counter() - started
        if (
            len(truth) != test.height
            or len(prediction) != test.height
            or not np.array_equal(truth, expected_truth)
        ):
            raise RuntimeError(
                f"satellite value fold {evaluation}/{fold} returned predictions for different test rows"
            )
        if not np.isfinite(prediction).all():
            raise RuntimeError(
                f"satellite value fold {evaluation}/{fold} returned non-finite predictions"
            )
        metrics = evaluate_predictions(truth, prediction, exceedance_threshold=None)
        if not all(math.isfinite(value) for value in (metrics.rmse, metrics.mae, metrics.r2)):
            raise RuntimeError(
                f"satellite value fold {evaluation}/{fold} returned non-finite metrics"
            )
        rows.append(
            {
                "evaluation": evaluation,
                "fold": fold,
                "quarter_fold": quarter_fold,
                "station_fold": station_fold,
                "feature_set": feature_set,
                "n_train": train.height,
                "n_test": test.height,
                "rmse": metrics.rmse,
                "mae": metrics.mae,
                "r2": metrics.r2,
                "fit_seconds": elapsed,
            }
        )
    return rows


def _assert_each_row_tested_once(
    frame: pl.DataFrame,
    test_frames: list[pl.DataFrame],
    *,
    evaluation: str,
) -> None:
    expected = sorted(frame.select("station_name", "month").iter_rows())
    observed = sorted(
        key for test in test_frames for key in test.select("station_name", "month").iter_rows()
    )
    if observed != expected:
        raise RuntimeError(f"satellite {evaluation} does not test every common row exactly once")


def evaluate_satellite_value(
    frame: pl.DataFrame,
    membership: pl.DataFrame,
    *,
    config: SatelliteValueConfig,
) -> pl.DataFrame:
    required = {
        "station_name",
        "month",
        "quarter_fold",
        "PM2.5",
        *(name for features in SATELLITE_FEATURE_SETS.values() for name in features),
    }
    _require_columns(frame, required, label="satellite common comparison frame")
    _require_columns(
        membership,
        ("station_name", "station_fold"),
        label="satellite station-fold membership",
    )
    if membership["station_name"].n_unique() != membership.height:
        raise RuntimeError("satellite station-fold membership is duplicated")
    if set(frame["station_name"].unique().to_list()) != set(membership["station_name"].to_list()):
        raise RuntimeError("satellite station-fold membership does not match common rows")
    with_folds = frame.join(
        membership.select("station_name", "station_fold"),
        on="station_name",
        how="left",
    ).sort("station_name", "month")
    rows: list[dict[str, object]] = []

    quarter_tests: list[pl.DataFrame] = []
    for quarter in range(config.quarter_folds):
        train = with_folds.filter(pl.col("quarter_fold") != quarter)
        test = with_folds.filter(pl.col("quarter_fold") == quarter)
        quarter_tests.append(test)
        rows.extend(
            _score_fold(
                train,
                test,
                evaluation="quarter_transfer",
                fold=f"quarter_{quarter}",
                quarter_fold=quarter,
                station_fold=None,
                model=config.model,
            )
        )
    _assert_each_row_tested_once(with_folds, quarter_tests, evaluation="quarter transfer")

    station_folds = sorted(membership["station_fold"].unique().to_list())
    station_tests: list[pl.DataFrame] = []
    for station_fold in station_folds:
        train = with_folds.filter(pl.col("station_fold") != station_fold)
        test = with_folds.filter(pl.col("station_fold") == station_fold)
        station_tests.append(test)
        rows.extend(
            _score_fold(
                train,
                test,
                evaluation="station_transfer",
                fold=f"station_{int(station_fold):02d}",
                quarter_fold=None,
                station_fold=int(station_fold),
                model=config.model,
            )
        )
    _assert_each_row_tested_once(with_folds, station_tests, evaluation="station transfer")

    combined_tests: list[pl.DataFrame] = []
    for quarter in range(config.quarter_folds):
        for station_fold in station_folds:
            train = with_folds.filter(
                (pl.col("quarter_fold") != quarter) & (pl.col("station_fold") != station_fold)
            )
            test = with_folds.filter(
                (pl.col("quarter_fold") == quarter) & (pl.col("station_fold") == station_fold)
            )
            combined_tests.append(test)
            rows.extend(
                _score_fold(
                    train,
                    test,
                    evaluation="spatiotemporal_transfer",
                    fold=f"quarter_{quarter}_station_{int(station_fold):02d}",
                    quarter_fold=quarter,
                    station_fold=int(station_fold),
                    model=config.model,
                )
            )
    _assert_each_row_tested_once(
        with_folds,
        combined_tests,
        evaluation="spatiotemporal transfer",
    )
    return pl.DataFrame(
        rows,
        schema_overrides={"quarter_fold": pl.Int64, "station_fold": pl.Int64},
    ).sort("evaluation", "fold", "feature_set")


def satellite_metric_deltas(scores: pl.DataFrame) -> pl.DataFrame:
    required = (
        "evaluation",
        "fold",
        "quarter_fold",
        "station_fold",
        "feature_set",
        "n_train",
        "n_test",
        "rmse",
        "mae",
        "r2",
    )
    _require_columns(scores, required, label="satellite value scores")
    group_columns = ("evaluation", "fold", "quarter_fold", "station_fold")
    rows: list[dict[str, object]] = []
    for group in scores.partition_by(*group_columns, maintain_order=True):
        identity = {column: group[column][0] for column in group_columns}
        indexed = {str(item["feature_set"]): item for item in group.iter_rows(named=True)}
        if set(indexed) != set(SATELLITE_FEATURE_SETS):
            raise RuntimeError(f"satellite value scores are incomplete for {identity}")
        for candidate_name, reference_name in _COMPARISONS:
            candidate = indexed[candidate_name]
            reference = indexed[reference_name]
            if (candidate["n_train"], candidate["n_test"]) != (
                reference["n_train"],
                reference["n_test"],
            ):
                raise RuntimeError(f"satellite value score rows are not paired for {identity}")
            rmse_delta = float(candidate["rmse"]) - float(reference["rmse"])
            mae_delta = float(candidate["mae"]) - float(reference["mae"])
            r2_delta = float(candidate["r2"]) - float(reference["r2"])
            rows.append(
                {
                    **identity,
                    "comparison": f"{candidate_name}_minus_{reference_name}",
                    "candidate": candidate_name,
                    "reference": reference_name,
                    "n_train": int(candidate["n_train"]),
                    "n_test": int(candidate["n_test"]),
                    "rmse_delta": rmse_delta,
                    "mae_delta": mae_delta,
                    "r2_delta": r2_delta,
                    "rmse_improved": rmse_delta < 0,
                    "r2_improved": r2_delta > 0,
                }
            )
    return pl.DataFrame(
        rows,
        schema_overrides={"quarter_fold": pl.Int64, "station_fold": pl.Int64},
    ).sort("evaluation", "fold", "comparison")


def _summarise_comparisons(deltas: pl.DataFrame) -> dict[str, Any]:
    comparisons: dict[str, Any] = {}
    for comparison in sorted(deltas["comparison"].unique().to_list()):
        subset = deltas.filter(pl.col("comparison") == comparison)
        both_improved = pl.col("rmse_improved") & pl.col("r2_improved")
        both_worse = (~pl.col("rmse_improved")) & (~pl.col("r2_improved"))
        exact_tie = (pl.col("rmse_delta") == 0) & (pl.col("r2_delta") == 0)
        counts = subset.select(
            both_improved.sum().alias("both_improved"),
            (both_worse & ~exact_tie).sum().alias("both_worse"),
            exact_tie.sum().alias("exact_tie"),
        ).row(0, named=True)
        known = (
            as_int(counts["both_improved"])
            + as_int(counts["both_worse"])
            + as_int(counts["exact_tie"])
        )
        comparisons[str(comparison)] = {
            "folds": subset.height,
            "test_rows": as_int(subset["n_test"].sum()),
            "median_rmse_delta": as_float(subset["rmse_delta"].median()),
            "median_mae_delta": as_float(subset["mae_delta"].median()),
            "median_r2_delta": as_float(subset["r2_delta"].median()),
            "both_improved": as_int(counts["both_improved"]),
            "both_worse": as_int(counts["both_worse"]),
            "exact_tie": as_int(counts["exact_tie"]),
            "mixed": subset.height - known,
        }
    return comparisons


def summarise_satellite_deltas(deltas: pl.DataFrame) -> dict[str, Any]:
    evaluations = {
        str(evaluation): _summarise_comparisons(deltas.filter(pl.col("evaluation") == evaluation))
        for evaluation in sorted(deltas["evaluation"].unique().to_list())
    }
    return {"overall": _summarise_comparisons(deltas), "evaluations": evaluations}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def satellite_value_dir(*, year: int, generation: str) -> Path:
    identity = validate_generation_sha256(generation)
    return outputs_dir("m8_satellite_value") / "generations" / identity / f"year={year}"


def _recover_satellite_value_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1 or len(stages) > 1:
        raise RuntimeError(f"multiple interrupted satellite value swaps found beside {destination}")
    if destination.exists() and backups and stages:
        raise RuntimeError(f"ambiguous interrupted satellite value swap found beside {destination}")
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    if destination.exists() and backups:
        shutil.rmtree(backups[0])


def write_satellite_value_result(
    result: SatelliteValueResult,
    *,
    destination: Path | None = None,
) -> dict[str, Path]:
    if destination is None:
        year = result.manifest.get("year")
        generation = result.manifest.get("inventory_generation_sha256")
        if not isinstance(year, int) or not isinstance(generation, str):
            raise RuntimeError("satellite value result lacks its output identity")
        out = satellite_value_dir(year=year, generation=generation)
    else:
        out = destination
    _recover_satellite_value_swap(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = out.with_name(f".{out.name}.staging-{token}")
    backup = out.with_name(f".{out.name}.backup-{token}")
    staged.mkdir()
    had_existing = out.exists()
    try:
        result.scores.write_parquet(staged / "scores.parquet")
        result.deltas.write_parquet(staged / "paired_deltas.parquet")
        result.coverage.write_parquet(staged / "coverage.parquet")
        result.station_folds.write_parquet(staged / "station_folds.parquet")
        _write_json(staged / "summary.json", result.summary)
        _write_json(staged / "manifest.json", result.manifest)
        try:
            if had_existing:
                out.replace(backup)
            staged.replace(out)
        except BaseException:
            if had_existing and backup.exists() and not out.exists():
                backup.replace(out)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return {
        "scores": out / "scores.parquet",
        "paired_deltas": out / "paired_deltas.parquet",
        "coverage": out / "coverage.parquet",
        "station_folds": out / "station_folds.parquet",
        "summary": out / "summary.json",
        "manifest": out / "manifest.json",
    }


def _input_file(path: Path) -> InputFile:
    if not path.is_file():
        raise FileNotFoundError(f"satellite value input not found: {path}")
    payload = path.read_bytes()
    return InputFile(path=path, bytes=len(payload), sha256=sha256(payload).hexdigest())


def _expected_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeError(f"{label} does not record a SHA-256")
    return value


def _upstream_inputs(
    upstream: Mapping[str, object],
    *,
    data_root: Path,
) -> tuple[InputFile, ...]:
    items: list[InputFile] = []
    ground = upstream.get("ground")
    if not isinstance(ground, dict) or not isinstance(ground.get("path"), str):
        raise RuntimeError("M8 upstream ground provenance is incomplete")
    ground_input = _input_file(data_root / ground["path"])
    recorded_ground = _expected_sha256(ground.get("sha256"), label="M8 upstream ground provenance")
    if ground_input.sha256 != recorded_ground:
        raise RuntimeError("M8 upstream ground SHA-256 no longer matches its manifest")
    items.append(ground_input)

    for source in ("s5p", "maiac"):
        provenance = upstream.get(source)
        if not isinstance(provenance, dict) or not isinstance(provenance.get("path"), str):
            raise RuntimeError(f"M8 upstream {source} provenance is incomplete")
        files = provenance.get("files")
        if not isinstance(files, dict) or not files:
            raise RuntimeError(f"M8 upstream {source} provenance has no files")
        source_root = data_root / provenance["path"]
        for name, recorded in sorted(files.items()):
            if not isinstance(name, str) or not name:
                raise RuntimeError(f"M8 upstream {source} has an invalid file name")
            item = _input_file(source_root / name)
            if item.sha256 != _expected_sha256(recorded, label=f"M8 upstream {source}/{name}"):
                raise RuntimeError(f"M8 upstream {source}/{name} SHA-256 no longer matches")
            items.append(item)
    indexed = {item.path: item for item in items}
    if len(indexed) != len(items):
        raise RuntimeError("M8 upstream provenance repeats an input path")
    return tuple(indexed[path] for path in sorted(indexed))


def _manifest_inputs(files: tuple[InputFile, ...], *, data_root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": item.path.relative_to(data_root).as_posix(),
            "bytes": item.bytes,
            "sha256": item.sha256,
        }
        for item in files
    ]


def run_satellite_value(
    *,
    data_root: Path,
    generation_sha256: str,
    config: SatelliteValueConfig | None = None,
    generated_at: str | None = None,
) -> SatelliteValueResult:
    identity = validate_generation_sha256(generation_sha256)
    selected = config or load_satellite_value_config()
    if data_root.resolve() != configured_data_root().resolve():
        raise RuntimeError(
            "satellite value data_root must match the configured data root used by M8"
        )
    association: SatelliteAssociationResult = run_satellite_association(
        year=selected.year,
        generation=identity,
    )
    if association.manifest.get("year") != selected.year:
        raise RuntimeError("M8 association year does not match satellite value config")
    if association.manifest.get("inventory_generation_sha256") != identity:
        raise RuntimeError("M8 association generation does not match the requested inventory")
    station_path = data_root / "outputs" / "qc" / "stations.parquet"
    station_input = _input_file(station_path)
    inventory = pl.read_parquet(station_path)
    if station_input != _input_file(station_path):
        raise RuntimeError("satellite value station inventory changed while it was read")
    measured_generation = station_inventory_generation(inventory).sha256
    if measured_generation != identity:
        raise RuntimeError(
            "satellite value station inventory generation does not match the requested generation"
        )
    upstream = association.manifest.get("upstream")
    if not isinstance(upstream, dict):
        raise RuntimeError("M8 association lacks immutable upstream provenance")
    source_inputs = _upstream_inputs(upstream, data_root=data_root)
    inputs = tuple(sorted((*source_inputs, station_input), key=lambda item: item.path))

    prepared = prepare_satellite_value_rows(
        association.panel,
        inventory,
        config=selected,
    )
    scores = evaluate_satellite_value(
        prepared.values,
        prepared.station_folds,
        config=selected,
    )
    deltas = satellite_metric_deltas(scores)
    summary = summarise_satellite_deltas(deltas)

    after = tuple(_input_file(item.path) for item in inputs)
    if inputs != after:
        raise RuntimeError("a satellite value input changed while models were fitting")

    run_at = generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    git_sha, git_dirty = git_state()
    coverage = prepared.coverage.row(0, named=True)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "complete": True,
        "generated_at": run_at,
        "year": selected.year,
        "inventory_generation_sha256": identity,
        "common_complete_rows": as_int(coverage["common_complete_rows"]),
        "station_fold_count": prepared.station_folds["station_fold"].n_unique(),
        "station_fold_method": "airzone_sorted_round_robin_with_unclassified_stratum",
        "unclassified_airzone_station_count": prepared.station_folds.filter(
            pl.col("airzone_official").is_null()
            | (pl.col("airzone_official").cast(pl.String, strict=False).str.strip_chars() == "")
        ).height,
        "quarter_fold_count": selected.quarter_folds,
        "feature_sets": {name: list(features) for name, features in SATELLITE_FEATURE_SETS.items()},
        "model": asdict(selected.model),
        "input_files": _manifest_inputs(inputs, data_root=data_root),
        "score_rows": scores.height,
        "delta_rows": deltas.height,
        "evaluations": sorted(scores["evaluation"].unique().to_list()),
        "limitations": [
            "descriptive held-out prediction within 2025, not causal attribution",
            "not a satellite PM2.5 calibration product or fused concentration field",
            "held-quarter transfer is seasonal blocking, not future-year forecasting",
            "not a replacement for M4 meteorological normalisation",
        ],
        "git_sha": git_sha,
        "git_dirty": git_dirty,
    }
    return SatelliteValueResult(
        scores=scores,
        deltas=deltas,
        coverage=prepared.coverage,
        station_folds=prepared.station_folds,
        summary=summary,
        manifest=manifest,
    )
