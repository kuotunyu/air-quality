"""Test whether measured ERA5 prediction value transfers across years and stations."""

from __future__ import annotations

import json
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

from twair.analysis.drivers import build_modelling_frame
from twair.analysis.era5_value import (
    ERA5_VALUE_FEATURE_SETS,
    Era5ValueConfig,
    InputFile,
    ModelConfig,
    PairedRows,
    TimeFold,
    evaluate_paired_models,
    load_era5_value_config,
    load_local_era5_year,
    prepare_paired_rows,
    station_scope,
    summarise_metric_deltas,
)
from twair.config import ConfigError, load_conf
from twair.models.evaluate import evaluate_predictions
from twair.paths import outputs_dir
from twair.provenance import git_state

__all__ = [
    "Era5RobustnessConfig",
    "Era5RobustnessResult",
    "annual_expanding_folds",
    "assign_station_folds",
    "evaluate_same_station_transfer",
    "evaluate_station_fold_transfer",
    "load_era5_robustness_config",
    "robustness_metric_deltas",
    "run_era5_robustness",
    "summarise_robustness_deltas",
    "write_era5_robustness_result",
]


_COMPARISONS: tuple[tuple[str, str, str], ...] = (
    ("combined", "local_weather", "combined_minus_local"),
    ("era5_weather", "local_weather", "era5_minus_local"),
    ("local_weather", "temporal_only", "local_minus_temporal"),
)

_SCORE_COLUMNS: tuple[str, ...] = (
    "evaluation",
    "train_year",
    "test_year",
    "station_fold",
    "station_name",
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
class Era5RobustnessConfig:
    years: tuple[int, int]
    station_folds: int
    pilot_stations: tuple[str, ...]
    model: ModelConfig


@dataclass(frozen=True, slots=True)
class Era5RobustnessResult:
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


def annual_expanding_folds(year: int) -> tuple[TimeFold, ...]:
    if isinstance(year, bool) or not isinstance(year, int) or year <= 0:
        raise ValueError("ERA5 robustness year must be a positive integer")
    return (
        TimeFold(
            name="q2",
            train_start=datetime(year, 1, 1),
            train_end=datetime(year, 4, 1),
            test_start=datetime(year, 4, 1),
            test_end=datetime(year, 7, 1),
        ),
        TimeFold(
            name="q3",
            train_start=datetime(year, 1, 1),
            train_end=datetime(year, 7, 1),
            test_start=datetime(year, 7, 1),
            test_end=datetime(year, 10, 1),
        ),
        TimeFold(
            name="q4",
            train_start=datetime(year, 1, 1),
            train_end=datetime(year, 10, 1),
            test_start=datetime(year, 10, 1),
            test_end=datetime(year + 1, 1, 1),
        ),
    )


def load_era5_robustness_config(
    config: dict[str, Any] | None = None,
) -> Era5RobustnessConfig:
    raw = config if config is not None else load_conf("era5_robustness")
    group = _mapping(raw.get("analysis"), path="era5_robustness.analysis")
    raw_years = group.get("years")
    if (
        not isinstance(raw_years, list)
        or len(raw_years) != 2
        or any(
            isinstance(year, bool) or not isinstance(year, int) or year <= 0 for year in raw_years
        )
    ):
        raise ConfigError("era5_robustness.analysis.years must contain two positive years")
    years = (raw_years[0], raw_years[1])
    if years[1] != years[0] + 1:
        raise ConfigError("era5_robustness.analysis.years must be consecutive and ascending")
    station_folds = group.get("station_folds")
    if isinstance(station_folds, bool) or not isinstance(station_folds, int) or station_folds < 2:
        raise ConfigError("era5_robustness.analysis.station_folds must be at least two")

    compatibility = {
        "analysis": {
            "year": years[1],
            "pilot_stations": group.get("pilot_stations"),
            "folds": [
                {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in asdict(fold).items()
                }
                for fold in annual_expanding_folds(years[1])
            ],
            "model": group.get("model"),
        }
    }
    validated = load_era5_value_config(compatibility)
    return Era5RobustnessConfig(
        years=years,
        station_folds=station_folds,
        pilot_stations=validated.pilot_stations,
        model=validated.model,
    )


def _require_columns(frame: pl.DataFrame, columns: Iterable[str], *, label: str) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise RuntimeError(f"{label} is missing {sorted(missing)}")


def assign_station_folds(inventory: pl.DataFrame, *, fold_count: int) -> pl.DataFrame:
    """Assign every station once while spreading each air zone across folds."""
    _require_columns(
        inventory,
        ("station_name", "airzone_official"),
        label="ERA5 robustness station inventory",
    )
    if isinstance(fold_count, bool) or not isinstance(fold_count, int) or fold_count < 2:
        raise ValueError("station fold count must be at least two")
    selected = inventory.select("station_name", "airzone_official")
    if selected["station_name"].n_unique() != selected.height:
        raise RuntimeError("ERA5 robustness station membership has duplicated station names")
    invalid = selected.filter(
        pl.col("station_name").is_null()
        | (pl.col("station_name").cast(pl.Utf8, strict=False).str.strip_chars() == "")
    )
    if not invalid.is_empty():
        raise RuntimeError("ERA5 robustness station membership has a missing name")
    if selected.height < fold_count:
        raise RuntimeError("ERA5 robustness has fewer stations than station folds")

    selected = selected.with_columns(
        pl.when(
            pl.col("airzone_official").is_null()
            | (pl.col("airzone_official").cast(pl.Utf8, strict=False).str.strip_chars() == "")
        )
        .then(pl.lit(None, dtype=pl.Utf8))
        .otherwise(pl.col("airzone_official").cast(pl.Utf8, strict=False).str.strip_chars())
        .alias("_fold_stratum")
    )
    rows: list[dict[str, object]] = []
    position = 0
    strata = selected.select("_fold_stratum").unique().sort("_fold_stratum", nulls_last=True)
    for stratum in strata["_fold_stratum"].to_list():
        in_stratum = selected.filter(
            pl.col("_fold_stratum").is_null()
            if stratum is None
            else pl.col("_fold_stratum") == stratum
        )
        for item in in_stratum.sort("station_name").iter_rows(named=True):
            rows.append(
                {
                    "station_name": item["station_name"],
                    "airzone_official": item["airzone_official"],
                    "station_fold": position % fold_count,
                }
            )
            position += 1
    membership = pl.DataFrame(rows).sort("station_name")
    observed_folds = sorted(membership["station_fold"].unique().to_list())
    if observed_folds != list(range(fold_count)):
        raise RuntimeError("ERA5 robustness station fold assignment produced an empty fold")
    return membership


def _single_year(frame: pl.DataFrame, *, label: str) -> int:
    _require_columns(frame, ("station_name", "ts_local", "PM2.5"), label=label)
    years = frame.select(pl.col("ts_local").dt.year().unique().sort())["ts_local"].to_list()
    if len(years) != 1:
        raise RuntimeError(f"{label} must contain exactly one local-calendar year")
    return int(years[0])


def _fit_predict(
    train: pl.DataFrame,
    test: pl.DataFrame,
    features: tuple[str, ...],
    model: ModelConfig,
) -> tuple[np.ndarray, np.ndarray]:
    from twair.analysis.era5_value import _fit_predict as fit_predict

    return fit_predict(train, test, features, model)


def _score_row(
    *,
    evaluation: str,
    train_year: int,
    test_year: int,
    station_fold: int | None,
    station_name: str,
    fold: str,
    feature_set: str,
    n_train: int,
    truth: np.ndarray,
    prediction: np.ndarray,
    fit_seconds: float,
) -> dict[str, object]:
    metrics = evaluate_predictions(truth, prediction, exceedance_threshold=None)
    return {
        "evaluation": evaluation,
        "train_year": train_year,
        "test_year": test_year,
        "station_fold": station_fold,
        "station_name": station_name,
        "fold": fold,
        "feature_set": feature_set,
        "n_train": n_train,
        "n_test": len(truth),
        "rmse": metrics.rmse,
        "mae": metrics.mae,
        "r2": metrics.r2,
        "fit_seconds": fit_seconds,
    }


def evaluate_same_station_transfer(
    train: pl.DataFrame,
    test: pl.DataFrame,
    *,
    model: ModelConfig,
) -> pl.DataFrame:
    train_year = _single_year(train, label="ERA5 temporal-transfer train frame")
    test_year = _single_year(test, label="ERA5 temporal-transfer test frame")
    if train_year >= test_year:
        raise RuntimeError("ERA5 temporal transfer must train before its test year")
    train_stations = set(train["station_name"].unique().to_list())
    test_stations = set(test["station_name"].unique().to_list())
    if not train_stations or train_stations != test_stations:
        raise RuntimeError("ERA5 temporal transfer requires the same non-empty station set")
    train_max = train["ts_local"].max()
    test_min = test["ts_local"].min()
    if (
        not isinstance(train_max, datetime)
        or not isinstance(test_min, datetime)
        or train_max >= test_min
    ):
        raise RuntimeError("ERA5 temporal transfer is not strictly forward in time")

    rows: list[dict[str, object]] = []
    for station in sorted(train_stations):
        station_train = train.filter(pl.col("station_name") == station)
        station_test = test.filter(pl.col("station_name") == station)
        for feature_set, features in ERA5_VALUE_FEATURE_SETS.items():
            started = perf_counter()
            truth, prediction = _fit_predict(station_train, station_test, features, model)
            rows.append(
                _score_row(
                    evaluation="temporal_transfer",
                    train_year=train_year,
                    test_year=test_year,
                    station_fold=None,
                    station_name=str(station),
                    fold=f"{train_year}_to_{test_year}",
                    feature_set=feature_set,
                    n_train=station_train.height,
                    truth=truth,
                    prediction=prediction,
                    fit_seconds=perf_counter() - started,
                )
            )
    return pl.DataFrame(rows, schema_overrides={"station_fold": pl.Int64}).sort(
        "station_name", "feature_set"
    )


def _validate_membership(
    train: pl.DataFrame,
    test: pl.DataFrame,
    membership: pl.DataFrame,
) -> None:
    _require_columns(
        membership,
        ("station_name", "airzone_official", "station_fold"),
        label="ERA5 robustness station membership",
    )
    if membership["station_name"].n_unique() != membership.height:
        raise RuntimeError("ERA5 robustness station membership is duplicated")
    members = set(membership["station_name"].to_list())
    if members != set(train["station_name"].unique().to_list()) or members != set(
        test["station_name"].unique().to_list()
    ):
        raise RuntimeError("ERA5 robustness station membership does not match train and test rows")
    folds = sorted(membership["station_fold"].unique().to_list())
    if len(folds) < 2 or folds != list(range(len(folds))):
        raise RuntimeError("ERA5 robustness station membership has missing or invalid folds")


def evaluate_station_fold_transfer(
    train: pl.DataFrame,
    test: pl.DataFrame,
    membership: pl.DataFrame,
    *,
    model: ModelConfig,
    evaluation: str,
) -> pl.DataFrame:
    if evaluation not in {"spatial_transfer", "spatiotemporal_transfer"}:
        raise ValueError("unknown ERA5 station-fold evaluation")
    train_year = _single_year(train, label=f"ERA5 {evaluation} train frame")
    test_year = _single_year(test, label=f"ERA5 {evaluation} test frame")
    if evaluation == "spatial_transfer" and train_year != test_year:
        raise RuntimeError("ERA5 spatial transfer must use one local-calendar year")
    if evaluation == "spatiotemporal_transfer" and train_year >= test_year:
        raise RuntimeError("ERA5 spatiotemporal transfer must train before its test year")
    _validate_membership(train, test, membership)

    rows: list[dict[str, object]] = []
    for fold in sorted(membership["station_fold"].unique().to_list()):
        held_out = membership.filter(pl.col("station_fold") == fold)["station_name"].to_list()
        train_rows = train.filter(~pl.col("station_name").is_in(held_out)).sort(
            "station_name", "ts_local"
        )
        test_rows = test.filter(pl.col("station_name").is_in(held_out)).sort(
            "station_name", "ts_local"
        )
        if train_rows.is_empty() or test_rows.is_empty():
            raise RuntimeError(f"ERA5 station fold {fold} has no train or test rows")
        if set(train_rows["station_name"].unique().to_list()) & set(held_out):
            raise RuntimeError(f"ERA5 station fold {fold} leaked a held-out station into training")
        if train_year < test_year:
            train_max = train_rows["ts_local"].max()
            test_min = test_rows["ts_local"].min()
            if (
                not isinstance(train_max, datetime)
                or not isinstance(test_min, datetime)
                or train_max >= test_min
            ):
                raise RuntimeError(f"ERA5 station fold {fold} is not forward in time")

        for feature_set, features in ERA5_VALUE_FEATURE_SETS.items():
            started = perf_counter()
            truth, prediction = _fit_predict(train_rows, test_rows, features, model)
            elapsed = perf_counter() - started
            expected_truth = test_rows["PM2.5"].to_numpy()
            if (
                len(truth) != test_rows.height
                or len(prediction) != test_rows.height
                or not np.array_equal(truth, expected_truth)
            ):
                raise RuntimeError(
                    f"ERA5 station fold {fold} returned predictions for different test rows"
                )
            predicted = test_rows.select("station_name", "ts_local", "PM2.5").with_columns(
                pl.Series("_prediction", prediction)
            )
            for station in sorted(held_out):
                station_rows = predicted.filter(pl.col("station_name") == station)
                rows.append(
                    _score_row(
                        evaluation=evaluation,
                        train_year=train_year,
                        test_year=test_year,
                        station_fold=int(fold),
                        station_name=str(station),
                        fold=f"station_fold_{int(fold):02d}",
                        feature_set=feature_set,
                        n_train=train_rows.height,
                        truth=station_rows["PM2.5"].to_numpy(),
                        prediction=station_rows["_prediction"].to_numpy(),
                        fit_seconds=elapsed,
                    )
                )
    return pl.DataFrame(rows).sort("station_fold", "station_name", "feature_set")


def _year_replication(
    frame: pl.DataFrame, *, year: int, config: Era5RobustnessConfig
) -> pl.DataFrame:
    selected = Era5ValueConfig(
        year=year,
        pilot_stations=config.pilot_stations,
        folds=annual_expanding_folds(year),
        model=config.model,
    )
    scores = evaluate_paired_models(frame, selected)
    return scores.with_columns(
        pl.lit("year_replication").alias("evaluation"),
        pl.lit(year).alias("train_year"),
        pl.lit(year).alias("test_year"),
        pl.lit(None, dtype=pl.Int64).alias("station_fold"),
    ).select(*_SCORE_COLUMNS)


def robustness_metric_deltas(scores: pl.DataFrame) -> pl.DataFrame:
    _require_columns(scores, _SCORE_COLUMNS, label="ERA5 robustness scores")
    group_columns = (
        "evaluation",
        "train_year",
        "test_year",
        "station_fold",
        "station_name",
        "fold",
    )
    rows: list[dict[str, object]] = []
    for group in scores.partition_by(list(group_columns), maintain_order=True):
        identity = {column: group[column][0] for column in group_columns}
        indexed = {str(row["feature_set"]): row for row in group.iter_rows(named=True)}
        if set(indexed) != set(ERA5_VALUE_FEATURE_SETS):
            raise RuntimeError(f"ERA5 robustness scores are incomplete for {identity}")
        for candidate_name, reference_name, comparison in _COMPARISONS:
            candidate = indexed[candidate_name]
            reference = indexed[reference_name]
            if (candidate["n_train"], candidate["n_test"]) != (
                reference["n_train"],
                reference["n_test"],
            ):
                raise RuntimeError(f"ERA5 robustness rows are not paired for {identity}")
            rmse_delta = float(candidate["rmse"]) - float(reference["rmse"])
            mae_delta = float(candidate["mae"]) - float(reference["mae"])
            r2_delta = float(candidate["r2"]) - float(reference["r2"])
            rows.append(
                {
                    **identity,
                    "comparison": comparison,
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
        "evaluation",
        "train_year",
        "test_year",
        "station_fold",
        "station_name",
        "fold",
        "comparison",
    )


def summarise_robustness_deltas(deltas: pl.DataFrame) -> dict[str, Any]:
    evaluations: dict[str, Any] = {}
    for evaluation in sorted(deltas["evaluation"].unique().to_list()):
        subset = deltas.filter(pl.col("evaluation") == evaluation)
        year_pairs: dict[str, Any] = {}
        for pair in subset.partition_by("train_year", "test_year", maintain_order=True):
            train_year = int(pair["train_year"][0])
            test_year = int(pair["test_year"][0])
            year_pairs[f"{train_year}_to_{test_year}"] = summarise_metric_deltas(pair)[
                "comparisons"
            ]
        evaluations[str(evaluation)] = {
            "overall": summarise_metric_deltas(subset)["comparisons"],
            "year_pairs": year_pairs,
        }
    return {"evaluations": evaluations}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_era5_robustness_result(
    result: Era5RobustnessResult,
    *,
    destination: Path | None = None,
) -> dict[str, Path]:
    out = destination or outputs_dir("m8_era5_robustness")
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
    except Exception:
        if staged.exists():
            shutil.rmtree(staged)
        raise
    return {
        "scores": out / "scores.parquet",
        "paired_deltas": out / "paired_deltas.parquet",
        "coverage": out / "coverage.parquet",
        "station_folds": out / "station_folds.parquet",
        "summary": out / "summary.json",
        "manifest": out / "manifest.json",
    }


def _input_file(path: Path) -> InputFile:
    payload = path.read_bytes()
    return InputFile(path=path, bytes=len(payload), sha256=sha256(payload).hexdigest())


def _observation_inputs(data_root: Path, *, year: int) -> tuple[InputFile, ...]:
    root = data_root / "processed" / "observations" / f"year={year}"
    paths = tuple(sorted(root.rglob("*.parquet"))) if root.is_dir() else ()
    if not paths:
        raise FileNotFoundError(f"canonical observation year not found: {root}")
    return tuple(_input_file(path) for path in paths)


def _unique_inputs(files: Iterable[InputFile]) -> tuple[InputFile, ...]:
    indexed: dict[Path, InputFile] = {}
    for item in files:
        previous = indexed.get(item.path)
        if previous is not None and previous != item:
            raise RuntimeError(f"ERA5 robustness input identity disagrees for {item.path}")
        indexed[item.path] = item
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


def _paired_year(
    data_root: Path,
    *,
    year: int,
    generation_sha256: str,
    stations: list[str] | None,
) -> tuple[PairedRows, tuple[InputFile, ...], tuple[str, ...], dict[str, Any]]:
    observations = _observation_inputs(data_root, year=year)
    era5 = load_local_era5_year(
        data_root,
        year=year,
        generation_sha256=generation_sha256,
    )
    local = build_modelling_frame(
        data_root / "processed" / "observations",
        period=(year, year),
        stations=stations,
    )
    era5_values = (
        era5.values.filter(pl.col("station_name").is_in(stations))
        if stations is not None
        else era5.values
    )
    paired = prepare_paired_rows(local, era5_values)
    candidates = (
        tuple(stations)
        if stations is not None
        else tuple(sorted(era5_values["station_name"].unique().to_list()))
    )
    scope = station_scope(
        candidate_stations=candidates,
        target_stations=local["station_name"].unique().to_list(),
        analyzed_stations=paired.values["station_name"].unique().to_list(),
    )
    return paired, (*observations, *era5.input_files), candidates, scope


def run_era5_robustness(
    *,
    data_root: Path,
    generation_sha256: str,
    pilot: bool,
    config: Era5RobustnessConfig | None = None,
    generated_at: str | None = None,
) -> Era5RobustnessResult:
    selected = config or load_era5_robustness_config()
    requested_stations = list(selected.pilot_stations) if pilot else None
    paired_by_year: dict[int, PairedRows] = {}
    scopes: dict[str, Any] = {}
    inputs: list[InputFile] = []
    coverage_frames: list[pl.DataFrame] = []
    for year in selected.years:
        paired, year_inputs, _candidates, scope = _paired_year(
            data_root,
            year=year,
            generation_sha256=generation_sha256,
            stations=requested_stations,
        )
        paired_by_year[year] = paired
        scopes[str(year)] = scope
        inputs.extend(year_inputs)
        coverage_frames.append(paired.coverage.with_columns(pl.lit(year).alias("year")))

    station_path = data_root / "outputs" / "qc" / "stations.parquet"
    station_before = _input_file(station_path)
    inventory = pl.read_parquet(station_path)
    if station_before != _input_file(station_path):
        raise RuntimeError("the station inventory changed while it was being read")
    inputs.append(station_before)

    first_year, second_year = selected.years
    stations_by_year = {
        year: set(paired.values["station_name"].unique().to_list())
        for year, paired in paired_by_year.items()
    }
    common_stations = tuple(sorted(stations_by_year[first_year] & stations_by_year[second_year]))
    if len(common_stations) < 2:
        raise RuntimeError("ERA5 robustness needs at least two stations analyzed in both years")
    effective_fold_count = min(selected.station_folds, len(common_stations))
    membership = assign_station_folds(
        inventory.filter(pl.col("station_name").is_in(common_stations)),
        fold_count=effective_fold_count,
    )
    common_frames = {
        year: paired.values.filter(pl.col("station_name").is_in(common_stations))
        for year, paired in paired_by_year.items()
    }

    score_frames = [
        _year_replication(paired_by_year[year].values, year=year, config=selected)
        for year in selected.years
    ]
    score_frames.extend(
        (
            evaluate_same_station_transfer(
                common_frames[first_year],
                common_frames[second_year],
                model=selected.model,
            ),
            evaluate_station_fold_transfer(
                common_frames[second_year],
                common_frames[second_year],
                membership,
                model=selected.model,
                evaluation="spatial_transfer",
            ),
            evaluate_station_fold_transfer(
                common_frames[first_year],
                common_frames[second_year],
                membership,
                model=selected.model,
                evaluation="spatiotemporal_transfer",
            ),
        )
    )
    scores = (
        pl.concat(score_frames, how="diagonal_relaxed")
        .select(*_SCORE_COLUMNS)
        .sort(
            "evaluation",
            "train_year",
            "test_year",
            "station_fold",
            "station_name",
            "fold",
            "feature_set",
        )
    )
    deltas = robustness_metric_deltas(scores)
    summary = summarise_robustness_deltas(deltas)

    all_before = _unique_inputs(inputs)
    all_after = tuple(_input_file(item.path) for item in all_before)
    if all_before != all_after:
        raise RuntimeError("an ERA5 robustness input changed while models were fitting")

    run_at = generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    git_sha, git_dirty = git_state()
    transfer_exclusions = [
        {
            "station_name": station,
            "reason": f"not_analyzed_in_both_{first_year}_and_{second_year}",
        }
        for station in sorted(stations_by_year[first_year] ^ stations_by_year[second_year])
    ]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "complete": True,
        "generated_at": run_at,
        "mode": "pilot" if pilot else "full",
        "years": list(selected.years),
        "station_scopes": scopes,
        "common_stations": list(common_stations),
        "transfer_exclusions": transfer_exclusions,
        "station_fold_count": effective_fold_count,
        "station_fold_method": "airzone_sorted_round_robin_with_unclassified_stratum",
        "unclassified_airzone_station_count": membership.filter(
            pl.col("airzone_official").is_null()
            | (pl.col("airzone_official").cast(pl.Utf8, strict=False).str.strip_chars() == "")
        ).height,
        "inventory_generation_sha256": generation_sha256,
        "feature_sets": {
            name: list(features) for name, features in ERA5_VALUE_FEATURE_SETS.items()
        },
        "annual_folds": {
            str(year): [
                {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in asdict(fold).items()
                }
                for fold in annual_expanding_folds(year)
            ]
            for year in selected.years
        },
        "model": asdict(selected.model),
        "input_files": _manifest_inputs(all_before, data_root=data_root),
        "paired_rows_by_year": {
            str(year): paired_by_year[year].values.height for year in selected.years
        },
        "score_rows": scores.height,
        "delta_rows": deltas.height,
        "evaluations": sorted(scores["evaluation"].unique().to_list()),
        "limitations": [
            "descriptive predictive generalisation, not causal attribution",
            "not calibration or sensor fusion",
            "not a replacement for M4 meteorological normalisation",
            "spatial transfer uses contemporaneous held-out-station rows",
        ],
        "git_sha": git_sha,
        "git_dirty": git_dirty,
    }
    return Era5RobustnessResult(
        scores=scores,
        deltas=deltas,
        coverage=pl.concat(coverage_frames, how="vertical_relaxed").select(
            "year", pl.exclude("year")
        ),
        station_folds=membership,
        summary=summary,
        manifest=manifest,
    )
