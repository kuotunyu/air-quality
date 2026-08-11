"""Measure whether held-out satellite prediction value persists across two years."""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import numpy as np
import polars as pl

from twair.analysis.era5_robustness import assign_station_folds
from twair.analysis.era5_value import InputFile, ModelConfig
from twair.analysis.satellite import SatelliteAssociationResult
from twair.analysis.satellite_value import (
    SATELLITE_FEATURE_SETS,
    SatelliteValueConfig,
    prepare_satellite_value_rows,
)
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
    "SatelliteRobustnessConfig",
    "SatelliteRobustnessResult",
    "SatelliteRobustnessRows",
    "evaluate_satellite_robustness",
    "load_satellite_robustness_config",
    "prepare_satellite_robustness_rows",
    "run_satellite_robustness",
    "satellite_robustness_metric_deltas",
    "summarise_satellite_robustness_deltas",
    "write_satellite_robustness_result",
]


_COMPARISONS: tuple[tuple[str, str], ...] = (
    ("baseline_aod", "baseline"),
    ("baseline_no2", "baseline"),
    ("baseline_so2", "baseline"),
    ("all_satellite", "baseline"),
)

_SCORE_COLUMNS: tuple[str, ...] = (
    "evaluation",
    "train_year",
    "test_year",
    "station_fold",
    "fold",
    "feature_set",
    "n_train",
    "n_test",
    "rmse",
    "mae",
    "r2",
    "fit_seconds",
)


@dataclass(frozen=True, slots=True)
class SatelliteRobustnessConfig:
    years: tuple[int, int]
    quarter_folds: int
    station_folds: int
    model: ModelConfig


@dataclass(frozen=True, slots=True)
class SatelliteRobustnessRows:
    values: pl.DataFrame
    yearly_values: dict[int, pl.DataFrame]
    coverage: pl.DataFrame
    station_folds: pl.DataFrame


@dataclass(frozen=True, slots=True)
class SatelliteRobustnessResult:
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


def load_satellite_robustness_config(
    config: dict[str, Any] | None = None,
) -> SatelliteRobustnessConfig:
    raw = config if config is not None else load_conf("satellite_robustness")
    group = _mapping(raw.get("analysis"), path="satellite_robustness.analysis")
    raw_years = group.get("years")
    if (
        not isinstance(raw_years, list)
        or len(raw_years) != 2
        or any(isinstance(year, bool) or not isinstance(year, int) for year in raw_years)
        or tuple(raw_years) != (2024, 2025)
    ):
        raise ConfigError("satellite_robustness.analysis.years must be [2024, 2025]")
    quarter_folds = _positive_int(
        group.get("quarter_folds"), path="satellite_robustness.analysis.quarter_folds"
    )
    if quarter_folds != 4:
        raise ConfigError("satellite_robustness.analysis.quarter_folds must be four")
    station_folds = _positive_int(
        group.get("station_folds"), path="satellite_robustness.analysis.station_folds"
    )
    if station_folds != 10:
        raise ConfigError("satellite_robustness.analysis.station_folds must be ten")
    raw_model = _mapping(group.get("model"), path="satellite_robustness.analysis.model")
    model = ModelConfig(
        n_estimators=_positive_int(
            raw_model.get("n_estimators"),
            path="satellite_robustness.analysis.model.n_estimators",
        ),
        learning_rate=_positive_float(
            raw_model.get("learning_rate"),
            path="satellite_robustness.analysis.model.learning_rate",
        ),
        num_leaves=_positive_int(
            raw_model.get("num_leaves"),
            path="satellite_robustness.analysis.model.num_leaves",
        ),
        min_child_samples=_positive_int(
            raw_model.get("min_child_samples"),
            path="satellite_robustness.analysis.model.min_child_samples",
        ),
        n_jobs=_positive_int(
            raw_model.get("n_jobs"), path="satellite_robustness.analysis.model.n_jobs"
        ),
        seed=_positive_int(raw_model.get("seed"), path="satellite_robustness.analysis.model.seed"),
    )
    if model.n_jobs != 1:
        raise ConfigError("satellite robustness analysis must use exactly one model job")
    if model != ModelConfig(
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=10,
        n_jobs=1,
        seed=20260811,
    ):
        raise ConfigError("satellite_robustness.analysis.model must match the reviewed protocol")
    return SatelliteRobustnessConfig(
        years=(2024, 2025),
        quarter_folds=quarter_folds,
        station_folds=station_folds,
        model=model,
    )


def _require_columns(frame: pl.DataFrame, required: Iterable[str], *, label: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise RuntimeError(f"{label} is missing required column(s): {missing}")


def _validate_association(
    association: SatelliteAssociationResult,
    *,
    year: int,
    generation: str | None = None,
) -> None:
    manifest = association.manifest
    if manifest.get("schema_version") != 1:
        raise RuntimeError(f"M8 association for {year} has an unsupported schema")
    if manifest.get("analysis") != "m8_satellite_association":
        raise RuntimeError(f"M8 association for {year} has the wrong analysis identity")
    if manifest.get("year") != year:
        raise RuntimeError(f"M8 association year does not match {year}")
    if manifest.get("mode") != "generation":
        raise RuntimeError(f"M8 association for {year} is not a generation output")
    if generation is not None and manifest.get("inventory_generation_sha256") != generation:
        raise RuntimeError(
            f"M8 association generation does not match the requested inventory for {year}"
        )


def prepare_satellite_robustness_rows(
    associations: dict[int, SatelliteAssociationResult],
    inventory: pl.DataFrame,
    *,
    config: SatelliteRobustnessConfig,
) -> SatelliteRobustnessRows:
    expected_years = set(config.years)
    if set(associations) != expected_years:
        raise RuntimeError("satellite robustness is missing a reviewed association year")
    values_by_year: dict[int, pl.DataFrame] = {}
    coverage_frames: list[pl.DataFrame] = []
    for year in config.years:
        association = associations[year]
        _validate_association(association, year=year)
        yearly = prepare_satellite_value_rows(
            association.panel,
            inventory,
            config=SatelliteValueConfig(
                year=year,
                quarter_folds=config.quarter_folds,
                station_folds=config.station_folds,
                model=config.model,
            ),
        )
        values_by_year[year] = yearly.values.with_columns(pl.lit(year).alias("year"))
        coverage_frames.append(yearly.coverage.with_columns(pl.lit(year).alias("year")))
    common_stations = set(values_by_year[config.years[0]]["station_name"].unique().to_list())
    for year in config.years[1:]:
        common_stations &= set(values_by_year[year]["station_name"].unique().to_list())
    if len(common_stations) < 2:
        raise RuntimeError("satellite robustness needs at least two stations in both years")
    common_names = sorted(str(station) for station in common_stations)
    effective_folds = min(config.station_folds, len(common_names))
    if effective_folds < 2:
        raise RuntimeError("satellite robustness needs at least two station folds")
    membership = assign_station_folds(
        inventory.filter(pl.col("station_name").is_in(common_names)),
        fold_count=effective_folds,
    )
    pre_cohort_values = pl.concat(
        [values_by_year[year] for year in config.years],
        how="vertical_relaxed",
    )
    values = pre_cohort_values.filter(pl.col("station_name").is_in(common_names)).sort(
        "year", "station_name", "month"
    )
    pre_cohort_coverage = pre_cohort_values.group_by("year").agg(
        pl.col("station_name").n_unique().alias("common_complete_stations")
    )
    cohort_coverage = values.group_by("year").agg(
        pl.len().alias("cross_year_common_rows"),
        pl.col("station_name").n_unique().alias("cross_year_common_stations"),
    )
    coverage = (
        pl.concat(coverage_frames, how="vertical_relaxed")
        .select("year", pl.exclude("year"))
        .join(pre_cohort_coverage, on="year", how="left")
        .join(cohort_coverage, on="year", how="left")
        .with_columns(
            (pl.col("common_complete_rows") - pl.col("cross_year_common_rows")).alias(
                "cross_year_excluded_rows"
            ),
            (pl.col("common_complete_stations") - pl.col("cross_year_common_stations")).alias(
                "cross_year_excluded_stations"
            ),
        )
    )
    return SatelliteRobustnessRows(
        values=values,
        yearly_values=values_by_year,
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
    train_year: int,
    test_year: int,
    station_fold: int | None,
    fold: str,
    model: ModelConfig,
) -> list[dict[str, object]]:
    if train.is_empty() or test.height < 2:
        raise RuntimeError(f"satellite robustness fold {evaluation}/{fold} has too few rows")
    keys = ("station_name", "month")
    if set(train.select(*keys).iter_rows()) & set(test.select(*keys).iter_rows()):
        raise RuntimeError(f"satellite robustness fold {evaluation}/{fold} leaked a test key")
    expected_truth = test["PM2.5"].to_numpy()
    rows: list[dict[str, object]] = []
    for feature_set, features in SATELLITE_FEATURE_SETS.items():
        started = perf_counter()
        truth, prediction = _fit_predict(train, test, features, model)
        if (
            len(truth) != test.height
            or len(prediction) != test.height
            or not np.array_equal(truth, expected_truth)
        ):
            raise RuntimeError(
                f"satellite robustness fold {evaluation}/{fold} returned predictions for different test rows"
            )
        if not np.isfinite(prediction).all():
            raise RuntimeError(
                f"satellite robustness fold {evaluation}/{fold} returned non-finite predictions"
            )
        metrics = evaluate_predictions(truth, prediction, exceedance_threshold=None)
        if not all(math.isfinite(value) for value in (metrics.rmse, metrics.mae, metrics.r2)):
            raise RuntimeError(
                f"satellite robustness fold {evaluation}/{fold} returned non-finite metrics"
            )
        rows.append(
            {
                "evaluation": evaluation,
                "train_year": train_year,
                "test_year": test_year,
                "station_fold": station_fold,
                "fold": fold,
                "feature_set": feature_set,
                "n_train": train.height,
                "n_test": test.height,
                "rmse": metrics.rmse,
                "mae": metrics.mae,
                "r2": metrics.r2,
                "fit_seconds": perf_counter() - started,
            }
        )
    return rows


def _validate_frame(frame: pl.DataFrame, *, config: SatelliteRobustnessConfig) -> None:
    feature_columns = tuple(
        dict.fromkeys(
            feature for features in SATELLITE_FEATURE_SETS.values() for feature in features
        )
    )
    _require_columns(
        frame,
        ("year", "station_name", "month", "quarter_fold", "PM2.5", *feature_columns),
        label="satellite robustness comparison frame",
    )
    if frame.is_empty() or set(frame["year"].unique().to_list()) != set(config.years):
        raise RuntimeError("satellite robustness comparison frame must include both reviewed years")
    if frame.select("year", "station_name", "month").is_duplicated().any():
        raise RuntimeError("satellite robustness comparison frame has duplicated row keys")
    if frame.select(*feature_columns, "PM2.5").null_count().sum_horizontal().item():
        raise RuntimeError("satellite robustness comparison frame has null model inputs")
    nonfinite = frame.select(
        *[
            (~pl.col(column).is_finite()).any().alias(column)
            for column in (*feature_columns, "PM2.5")
        ]
    ).row(0, named=True)
    if any(bool(value) for value in nonfinite.values()):
        raise RuntimeError("satellite robustness comparison frame has non-finite input values")


def _validate_membership(frame: pl.DataFrame, membership: pl.DataFrame) -> None:
    _require_columns(
        membership,
        ("station_name", "airzone_official", "station_fold"),
        label="satellite robustness station membership",
    )
    if membership["station_name"].n_unique() != membership.height:
        raise RuntimeError("satellite robustness station membership is duplicated")
    stations = set(frame["station_name"].unique().to_list())
    if stations != set(membership["station_name"].to_list()):
        raise RuntimeError("satellite robustness station membership does not match common rows")
    folds = sorted(membership["station_fold"].unique().to_list())
    if len(folds) < 2 or folds != list(range(len(folds))):
        raise RuntimeError("satellite robustness station membership has missing or invalid folds")


def _assert_each_row_tested_once(
    frame: pl.DataFrame,
    test_frames: list[pl.DataFrame],
    *,
    evaluation: str,
) -> None:
    expected = sorted(frame.select("year", "station_name", "month").iter_rows())
    observed = sorted(
        key
        for test in test_frames
        for key in test.select("year", "station_name", "month").iter_rows()
    )
    if observed != expected:
        raise RuntimeError(
            f"satellite robustness {evaluation} does not test every row exactly once"
        )


def evaluate_satellite_robustness(
    frame: pl.DataFrame,
    membership: pl.DataFrame,
    *,
    config: SatelliteRobustnessConfig,
    yearly_values: dict[int, pl.DataFrame] | None = None,
) -> pl.DataFrame:
    _validate_frame(frame, config=config)
    _validate_membership(frame, membership)
    if yearly_values is None:
        yearly_frame = frame
    else:
        if set(yearly_values) != set(config.years):
            raise RuntimeError(
                "satellite robustness year replication frames must include both years"
            )
        yearly_frame = pl.concat(
            [yearly_values[year] for year in config.years], how="vertical_relaxed"
        )
        _validate_frame(yearly_frame, config=config)
        for year in config.years:
            if set(yearly_values[year]["year"].unique().to_list()) != {year}:
                raise RuntimeError(
                    "satellite robustness year replication frame does not match its year"
                )
    with_folds = frame.join(
        membership.select("station_name", "station_fold"), on="station_name", how="left"
    ).sort("year", "station_name", "month")
    rows: list[dict[str, object]] = []
    for year in config.years:
        yearly = yearly_frame.filter(pl.col("year") == year)
        quarter_tests: list[pl.DataFrame] = []
        for quarter in range(config.quarter_folds):
            train = yearly.filter(pl.col("quarter_fold") != quarter)
            test = yearly.filter(pl.col("quarter_fold") == quarter)
            quarter_tests.append(test)
            rows.extend(
                _score_fold(
                    train,
                    test,
                    evaluation="year_replication",
                    train_year=year,
                    test_year=year,
                    station_fold=None,
                    fold=f"quarter_{quarter}",
                    model=config.model,
                )
            )
        _assert_each_row_tested_once(yearly, quarter_tests, evaluation="year replication")
    station_folds = sorted(membership["station_fold"].unique().to_list())
    for train_year, test_year in (
        (config.years[0], config.years[1]),
        (config.years[1], config.years[0]),
    ):
        train_yearly = with_folds.filter(pl.col("year") == train_year)
        test_yearly = with_folds.filter(pl.col("year") == test_year)
        direction = f"{train_year}_to_{test_year}"
        rows.extend(
            _score_fold(
                train_yearly,
                test_yearly,
                evaluation="cross_year_replication",
                train_year=train_year,
                test_year=test_year,
                station_fold=None,
                fold=direction,
                model=config.model,
            )
        )
        station_tests: list[pl.DataFrame] = []
        for station_fold in station_folds:
            held_out = membership.filter(pl.col("station_fold") == station_fold)[
                "station_name"
            ].to_list()
            train = train_yearly.filter(~pl.col("station_name").is_in(held_out))
            test = test_yearly.filter(pl.col("station_name").is_in(held_out))
            if set(train["station_name"].unique().to_list()) & set(held_out):
                raise RuntimeError(
                    f"satellite robustness station fold {station_fold} leaked a held-out station"
                )
            station_tests.append(test)
            rows.extend(
                _score_fold(
                    train,
                    test,
                    evaluation="station_year_transfer",
                    train_year=train_year,
                    test_year=test_year,
                    station_fold=int(station_fold),
                    fold=f"{direction}_station_{int(station_fold):02d}",
                    model=config.model,
                )
            )
        _assert_each_row_tested_once(
            test_yearly,
            station_tests,
            evaluation=f"station-year {direction}",
        )
    return (
        pl.DataFrame(rows, schema_overrides={"station_fold": pl.Int64})
        .select(*_SCORE_COLUMNS)
        .sort("evaluation", "train_year", "test_year", "station_fold", "fold", "feature_set")
    )


def satellite_robustness_metric_deltas(scores: pl.DataFrame) -> pl.DataFrame:
    _require_columns(scores, _SCORE_COLUMNS, label="satellite robustness scores")
    group_columns = ("evaluation", "train_year", "test_year", "station_fold", "fold")
    rows: list[dict[str, object]] = []
    for group in scores.partition_by(*group_columns, maintain_order=True):
        identity = {column: group[column][0] for column in group_columns}
        indexed = {str(row["feature_set"]): row for row in group.iter_rows(named=True)}
        if set(indexed) != set(SATELLITE_FEATURE_SETS):
            raise RuntimeError(f"satellite robustness scores are incomplete for {identity}")
        for candidate_name, reference_name in _COMPARISONS:
            candidate = indexed[candidate_name]
            reference = indexed[reference_name]
            if (candidate["n_train"], candidate["n_test"]) != (
                reference["n_train"],
                reference["n_test"],
            ):
                raise RuntimeError(f"satellite robustness score rows are not paired for {identity}")
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
    return pl.DataFrame(rows, schema_overrides={"station_fold": pl.Int64}).sort(
        "evaluation", "train_year", "test_year", "station_fold", "fold", "comparison"
    )


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


def summarise_satellite_robustness_deltas(deltas: pl.DataFrame) -> dict[str, Any]:
    evaluations: dict[str, Any] = {}
    for evaluation in sorted(deltas["evaluation"].unique().to_list()):
        subset = deltas.filter(pl.col("evaluation") == evaluation)
        pairs: dict[str, Any] = {}
        for pair in subset.partition_by("train_year", "test_year", maintain_order=True):
            train_year = int(pair["train_year"][0])
            test_year = int(pair["test_year"][0])
            pairs[f"{train_year}_to_{test_year}"] = _summarise_comparisons(pair)
        evaluations[str(evaluation)] = {
            "overall": _summarise_comparisons(subset),
            "year_pairs": pairs,
        }
    return {"evaluations": evaluations}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def satellite_robustness_dir(*, generation: str) -> Path:
    identity = validate_generation_sha256(generation)
    return outputs_dir("m8_satellite_robustness") / "generations" / identity


def _recover_satellite_robustness_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1 or len(stages) > 1:
        raise RuntimeError(
            f"multiple interrupted satellite robustness swaps found beside {destination}"
        )
    if destination.exists() and backups and stages:
        raise RuntimeError(
            f"ambiguous interrupted satellite robustness swap found beside {destination}"
        )
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    if destination.exists() and backups:
        shutil.rmtree(backups[0])


def write_satellite_robustness_result(
    result: SatelliteRobustnessResult,
    *,
    destination: Path | None = None,
) -> dict[str, Path]:
    if destination is None:
        generation = result.manifest.get("inventory_generation_sha256")
        if not isinstance(generation, str):
            raise RuntimeError("satellite robustness result lacks its output identity")
        out = satellite_robustness_dir(generation=generation)
    else:
        out = destination
    _recover_satellite_robustness_swap(out)
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
        raise FileNotFoundError(f"satellite robustness input not found: {path}")
    payload = path.read_bytes()
    return InputFile(path=path, bytes=len(payload), sha256=sha256(payload).hexdigest())


def _association_output_dir(data_root: Path, *, year: int, generation: str) -> Path:
    return data_root / "outputs" / "m8_satellite" / "generations" / generation / f"year={year}"


def _read_association_output(
    data_root: Path,
    *,
    year: int,
    generation: str,
) -> tuple[SatelliteAssociationResult, tuple[InputFile, ...], str]:
    destination = _association_output_dir(data_root, year=year, generation=generation)
    panel_path = destination / "panel.parquet"
    manifest_path = destination / "manifest.json"
    before = tuple(_input_file(path) for path in (panel_path, manifest_path))
    panel = pl.read_parquet(panel_path)
    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_raw, dict):
        raise RuntimeError(f"M8 association manifest for {year} must be an object")
    after = tuple(_input_file(path) for path in (panel_path, manifest_path))
    if before != after:
        raise RuntimeError(f"M8 association output changed while it was read for {year}")
    association = SatelliteAssociationResult(
        panel=panel,
        coverage=pl.DataFrame(),
        association=pl.DataFrame(),
        station_context=pl.DataFrame(),
        month_context=pl.DataFrame(),
        manifest=manifest_raw,
    )
    _validate_association(association, year=year, generation=generation)
    return association, before, destination.relative_to(data_root).as_posix()


def _manifest_inputs(files: tuple[InputFile, ...], *, data_root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": item.path.relative_to(data_root).as_posix(),
            "bytes": item.bytes,
            "sha256": item.sha256,
        }
        for item in files
    ]


def run_satellite_robustness(
    *,
    data_root: Path,
    generation_sha256: str,
    config: SatelliteRobustnessConfig | None = None,
    generated_at: str | None = None,
) -> SatelliteRobustnessResult:
    identity = validate_generation_sha256(generation_sha256)
    selected = config or load_satellite_robustness_config()
    if data_root.resolve() != configured_data_root().resolve():
        raise RuntimeError(
            "satellite robustness data_root must match the configured data root used by M8"
        )
    associations: dict[int, SatelliteAssociationResult] = {}
    association_inputs: list[InputFile] = []
    association_paths: dict[str, str] = {}
    for year in selected.years:
        association, files, relative = _read_association_output(
            data_root,
            year=year,
            generation=identity,
        )
        associations[year] = association
        association_inputs.extend(files)
        association_paths[str(year)] = relative
    station_path = data_root / "outputs" / "qc" / "stations.parquet"
    station_input = _input_file(station_path)
    inventory = pl.read_parquet(station_path)
    if station_input != _input_file(station_path):
        raise RuntimeError("satellite robustness station inventory changed while it was read")
    if station_inventory_generation(inventory).sha256 != identity:
        raise RuntimeError(
            "satellite robustness station inventory generation does not match the requested generation"
        )
    inputs = tuple(sorted((*association_inputs, station_input), key=lambda item: item.path))
    prepared = prepare_satellite_robustness_rows(associations, inventory, config=selected)
    scores = evaluate_satellite_robustness(
        prepared.values,
        prepared.station_folds,
        config=selected,
        yearly_values=prepared.yearly_values,
    )
    deltas = satellite_robustness_metric_deltas(scores)
    summary = summarise_satellite_robustness_deltas(deltas)
    after = tuple(_input_file(item.path) for item in inputs)
    if inputs != after:
        raise RuntimeError("a satellite robustness input changed while models were fitting")
    run_at = generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    git_sha, git_dirty = git_state()
    common_stations = sorted(prepared.station_folds["station_name"].to_list())
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "complete": True,
        "generated_at": run_at,
        "years": list(selected.years),
        "inventory_generation_sha256": identity,
        "association_inputs": association_paths,
        "common_stations": common_stations,
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
            "descriptive held-out prediction across two observed years, not causal attribution",
            "not a satellite PM2.5 calibration product or fused concentration field",
            "2025_to_2024 is reverse-direction cross-year replication",
            "not a replacement for M4 meteorological normalisation",
        ],
        "git_sha": git_sha,
        "git_dirty": git_dirty,
    }
    return SatelliteRobustnessResult(
        scores=scores,
        deltas=deltas,
        coverage=prepared.coverage,
        station_folds=prepared.station_folds,
        summary=summary,
        manifest=manifest,
    )
