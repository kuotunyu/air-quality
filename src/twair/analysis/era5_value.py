"""Measure whether ERA5 adds held-out information beyond station weather."""

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
from twair.config import ConfigError, load_conf
from twair.features.met import WIND_FEATURES
from twair.features.temporal import TEMPORAL_FEATURES
from twair.ingest.era5 import read_era5_result
from twair.ingest.station_inventory import validate_generation_sha256
from twair.models.evaluate import Split, evaluate_predictions
from twair.paths import outputs_dir
from twair.provenance import git_state
from twair.scalars import as_float, as_int

__all__ = [
    "ERA5_DERIVED_FEATURES",
    "ERA5_SOURCE_FEATURES",
    "ERA5_VALUE_FEATURE_SETS",
    "LOCAL_WEATHER_FEATURES",
    "TEMPORAL_VALUE_FEATURES",
    "Era5ValueConfig",
    "Era5ValueResult",
    "InputFile",
    "LocalEra5Year",
    "ModelConfig",
    "PairedRows",
    "TimeFold",
    "assemble_local_era5_year",
    "derive_era5_features",
    "evaluate_paired_models",
    "explicit_time_splits",
    "load_era5_value_config",
    "load_local_era5_year",
    "paired_metric_deltas",
    "prepare_paired_rows",
    "run_era5_value",
    "station_scope",
    "summarise_metric_deltas",
    "write_era5_value_result",
]


ERA5_SOURCE_FEATURES: tuple[str, ...] = (
    "blh_m",
    "u10_m_s",
    "v10_m_s",
    "t2m_k",
    "d2m_k",
    "sp_pa",
)

ERA5_DERIVED_FEATURES: tuple[str, ...] = (
    "era5_blh_m",
    "era5_u10_m_s",
    "era5_v10_m_s",
    "era5_wind_speed_m_s",
    "era5_t2m_c",
    "era5_rh_pct",
    "era5_sp_hpa",
)

LOCAL_WEATHER_FEATURES: tuple[str, ...] = (
    "AMB_TEMP",
    "RH",
    "RAINFALL",
    "WS_HR",
    *WIND_FEATURES,
)
TEMPORAL_VALUE_FEATURES: tuple[str, ...] = TEMPORAL_FEATURES
ERA5_VALUE_FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "temporal_only": TEMPORAL_VALUE_FEATURES,
    "local_weather": (*TEMPORAL_VALUE_FEATURES, *LOCAL_WEATHER_FEATURES),
    "era5_weather": (*TEMPORAL_VALUE_FEATURES, *ERA5_DERIVED_FEATURES),
    "combined": (
        *TEMPORAL_VALUE_FEATURES,
        *LOCAL_WEATHER_FEATURES,
        *ERA5_DERIVED_FEATURES,
    ),
}

_ERA5_KEY_COLUMNS: tuple[str, ...] = (
    "station_name",
    "ts_utc",
    "grid_lat",
    "grid_lon",
    "grid_distance_km",
)


@dataclass(frozen=True, slots=True)
class InputFile:
    path: Path
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class LocalEra5Year:
    values: pl.DataFrame
    inventory_generation_sha256: str
    input_files: tuple[InputFile, ...]


@dataclass(frozen=True, slots=True)
class TimeFold:
    name: str
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


@dataclass(frozen=True, slots=True)
class ModelConfig:
    n_estimators: int
    learning_rate: float
    num_leaves: int
    min_child_samples: int
    n_jobs: int
    seed: int


@dataclass(frozen=True, slots=True)
class Era5ValueConfig:
    year: int
    pilot_stations: tuple[str, ...]
    folds: tuple[TimeFold, ...]
    model: ModelConfig


@dataclass(frozen=True, slots=True)
class PairedRows:
    values: pl.DataFrame
    coverage: pl.DataFrame


@dataclass(frozen=True, slots=True)
class Era5ValueResult:
    scores: pl.DataFrame
    deltas: pl.DataFrame
    coverage: pl.DataFrame
    summary: dict[str, Any]
    manifest: dict[str, Any]


def _required_columns(frame: pl.DataFrame, columns: Iterable[str], *, label: str) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise RuntimeError(f"{label} is missing {sorted(missing)}")


def _mapping(value: object, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def _positive_int(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{path} must be a positive integer")
    return value


def _finite_positive_float(value: object, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a positive finite number")
    converted = float(value)
    if converted <= 0 or not pl.Series([converted]).is_finite().item():
        raise ConfigError(f"{path} must be a positive finite number")
    return converted


def _local_datetime(value: object, *, path: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} must be an ISO local datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{path} must be an ISO local datetime") from exc
    if parsed.tzinfo is not None:
        raise ConfigError(f"{path} must not carry a timezone offset")
    return parsed


def _datetime_scalar(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise RuntimeError(f"{label} must be a datetime")
    return value


def load_era5_value_config(config: dict[str, Any] | None = None) -> Era5ValueConfig:
    raw = config if config is not None else load_conf("era5_value")
    group = _mapping(raw.get("analysis"), path="era5_value.analysis")
    year = _positive_int(group.get("year"), path="era5_value.analysis.year")

    raw_stations = group.get("pilot_stations")
    if (
        not isinstance(raw_stations, list)
        or not raw_stations
        or any(not isinstance(name, str) or not name.strip() for name in raw_stations)
    ):
        raise ConfigError("era5_value.analysis.pilot_stations must be non-empty station names")
    pilot_stations = tuple(name.strip() for name in raw_stations)
    if len(pilot_stations) != len(set(pilot_stations)):
        raise ConfigError("era5_value.analysis.pilot_stations must be unique")

    raw_folds = group.get("folds")
    if not isinstance(raw_folds, list) or not raw_folds:
        raise ConfigError("era5_value.analysis.folds must be a non-empty list")
    folds: list[TimeFold] = []
    for index, value in enumerate(raw_folds):
        fold = _mapping(value, path=f"era5_value.analysis.folds[{index}]")
        name = fold.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"era5_value.analysis.folds[{index}].name must be non-empty")
        parsed = TimeFold(
            name=name.strip(),
            train_start=_local_datetime(
                fold.get("train_start"), path=f"era5_value.analysis.folds[{index}].train_start"
            ),
            train_end=_local_datetime(
                fold.get("train_end"), path=f"era5_value.analysis.folds[{index}].train_end"
            ),
            test_start=_local_datetime(
                fold.get("test_start"), path=f"era5_value.analysis.folds[{index}].test_start"
            ),
            test_end=_local_datetime(
                fold.get("test_end"), path=f"era5_value.analysis.folds[{index}].test_end"
            ),
        )
        if not (parsed.train_start < parsed.train_end <= parsed.test_start < parsed.test_end):
            raise ConfigError(f"era5_value.analysis.folds[{index}] has overlapping boundaries")
        folds.append(parsed)
    if len({fold.name for fold in folds}) != len(folds):
        raise ConfigError("era5_value.analysis fold names must be unique")

    raw_model = _mapping(group.get("model"), path="era5_value.analysis.model")
    model = ModelConfig(
        n_estimators=_positive_int(
            raw_model.get("n_estimators"), path="era5_value.analysis.model.n_estimators"
        ),
        learning_rate=_finite_positive_float(
            raw_model.get("learning_rate"), path="era5_value.analysis.model.learning_rate"
        ),
        num_leaves=_positive_int(
            raw_model.get("num_leaves"), path="era5_value.analysis.model.num_leaves"
        ),
        min_child_samples=_positive_int(
            raw_model.get("min_child_samples"),
            path="era5_value.analysis.model.min_child_samples",
        ),
        n_jobs=_positive_int(raw_model.get("n_jobs"), path="era5_value.analysis.model.n_jobs"),
        seed=_positive_int(raw_model.get("seed"), path="era5_value.analysis.model.seed"),
    )
    if model.n_jobs != 1:
        raise ConfigError("ERA5 value-add models must run serially with n_jobs=1")
    return Era5ValueConfig(
        year=year,
        pilot_stations=pilot_stations,
        folds=tuple(folds),
        model=model,
    )


def assemble_local_era5_year(
    frames: Iterable[pl.DataFrame],
    *,
    year: int,
    expected_stations: tuple[str, ...],
) -> pl.DataFrame:
    """Combine UTC source years and return one exact Asia/Taipei calendar year."""
    if isinstance(year, bool) or not isinstance(year, int) or year <= 0:
        raise ValueError("ERA5 local-calendar year must be a positive integer")
    if not expected_stations or len(expected_stations) != len(set(expected_stations)):
        raise ValueError("expected ERA5 station names must be non-empty and unique")

    source_frames = list(frames)
    if not source_frames:
        raise RuntimeError("ERA5 local-calendar assembly needs source frames")
    required = (*_ERA5_KEY_COLUMNS, *ERA5_SOURCE_FEATURES)
    for frame in source_frames:
        _required_columns(frame, required, label="ERA5 station-hour frame")

    combined = pl.concat(source_frames, how="vertical_relaxed")
    duplicated = combined.group_by("station_name", "ts_utc").len().filter(pl.col("len") > 1)
    if not duplicated.is_empty():
        raise RuntimeError("ERA5 station-hour keys are duplicated")

    local = combined.with_columns(
        pl.col("ts_utc")
        .dt.convert_time_zone("Asia/Taipei")
        .dt.replace_time_zone(None)
        .alias("ts_local")
    ).filter(pl.col("ts_local").dt.year() == year)

    observed_stations = set(local["station_name"].unique().to_list())
    if observed_stations != set(expected_stations):
        raise RuntimeError("ERA5 local-calendar station set does not match the reviewed inventory")

    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    expected_hours = pl.datetime_range(start, end, interval="1h", closed="left", eager=True)
    expected_keys = pl.DataFrame({"station_name": list(expected_stations)}).join(
        pl.DataFrame({"ts_local": expected_hours}),
        how="cross",
    )
    observed_keys = local.select("station_name", "ts_local")
    if (
        local.height != expected_keys.height
        or not expected_keys.join(
            observed_keys,
            on=["station_name", "ts_local"],
            how="anti",
        ).is_empty()
    ):
        raise RuntimeError("ERA5 source does not contain complete local-calendar hours")
    if observed_keys.unique().height != observed_keys.height:
        raise RuntimeError("ERA5 local-calendar station-hour keys are duplicated")

    return local.sort("station_name", "ts_local")


def derive_era5_features(frame: pl.DataFrame) -> pl.DataFrame:
    """Attach unit conversions and weather quantities without filling source nulls."""
    _required_columns(frame, ERA5_SOURCE_FEATURES, label="ERA5 feature frame")
    invalid = frame.filter(
        pl.any_horizontal(
            *(
                pl.col(name).is_not_null() & ~pl.col(name).cast(pl.Float64).is_finite()
                for name in ERA5_SOURCE_FEATURES
            )
        )
    )
    if not invalid.is_empty():
        raise RuntimeError("ERA5 feature frame contains a non-finite source value")

    temperature_c = pl.col("t2m_k") - 273.15
    dewpoint_c = pl.col("d2m_k") - 273.15
    featured = frame.with_columns(
        pl.col("blh_m").alias("era5_blh_m"),
        pl.col("u10_m_s").alias("era5_u10_m_s"),
        pl.col("v10_m_s").alias("era5_v10_m_s"),
        (pl.col("u10_m_s").pow(2) + pl.col("v10_m_s").pow(2)).sqrt().alias("era5_wind_speed_m_s"),
        temperature_c.alias("era5_t2m_c"),
        (
            100.0
            * (
                (17.625 * dewpoint_c / (243.04 + dewpoint_c))
                - (17.625 * temperature_c / (243.04 + temperature_c))
            ).exp()
        ).alias("era5_rh_pct"),
        (pl.col("sp_pa") / 100.0).alias("era5_sp_hpa"),
    )
    invalid_derived = featured.filter(
        pl.any_horizontal(
            *(
                pl.col(name).is_not_null() & ~pl.col(name).is_finite()
                for name in ERA5_DERIVED_FEATURES
            )
        )
    )
    if not invalid_derived.is_empty():
        raise RuntimeError("ERA5 derived feature is non-finite")
    return featured


def _input_file(path: Path) -> InputFile:
    payload = path.read_bytes()
    return InputFile(path=path, bytes=len(payload), sha256=sha256(payload).hexdigest())


def load_local_era5_year(
    data_root: Path,
    *,
    year: int,
    generation_sha256: str,
) -> LocalEra5Year:
    """Read validated ERA5 generations and bind the result to exact input bytes."""
    generation = validate_generation_sha256(generation_sha256)
    base = data_root / "interim" / "era5" / "generations" / generation
    destinations = (base / f"year={year - 1}", base / f"year={year}")
    paths = tuple(
        path
        for destination in destinations
        for path in (
            destination / "manifest.json",
            destination / "era5_station_hour.parquet",
        )
    )
    before = tuple(_input_file(path) for path in paths)
    prior, current = (read_era5_result(destination) for destination in destinations)
    after = tuple(_input_file(path) for path in paths)
    if before != after:
        raise RuntimeError("an ERA5 analysis input changed while it was being read")

    for result in (prior, current):
        if result.manifest.get("inventory_generation_sha256") != generation:
            raise RuntimeError("ERA5 input does not match the requested inventory generation")
    if prior.manifest.get("year") != year - 1 or 12 not in prior.manifest.get("months", []):
        raise RuntimeError("ERA5 prior UTC year does not include December")
    if current.manifest.get("year") != year or current.manifest.get("months") != list(range(1, 13)):
        raise RuntimeError("ERA5 current UTC year does not include all twelve months")
    station_count = current.manifest.get("stations_with_coordinates")
    if (
        isinstance(station_count, bool)
        or not isinstance(station_count, int)
        or station_count <= 0
        or prior.manifest.get("stations_with_coordinates") != station_count
    ):
        raise RuntimeError("ERA5 UTC years do not share one positive station count")
    expected_stations = tuple(sorted(current.values["station_name"].unique().to_list()))
    if len(expected_stations) != station_count:
        raise RuntimeError("ERA5 current UTC year does not match its station count")

    values = derive_era5_features(
        assemble_local_era5_year(
            (prior.values, current.values),
            year=year,
            expected_stations=expected_stations,
        )
    )
    return LocalEra5Year(
        values=values,
        inventory_generation_sha256=generation,
        input_files=before,
    )


def _complete_expr(columns: tuple[str, ...]) -> pl.Expr:
    return pl.all_horizontal(
        *(
            pl.col(name).is_not_null() & pl.col(name).cast(pl.Float64).is_finite()
            for name in columns
        )
    )


def prepare_paired_rows(local: pl.DataFrame, era5: pl.DataFrame) -> PairedRows:
    """Measure missingness, then retain one common row set for every model."""
    key_columns = ("station_name", "ts_local")
    _required_columns(
        local,
        (*key_columns, "PM2.5", *LOCAL_WEATHER_FEATURES, *TEMPORAL_VALUE_FEATURES),
        label="local modelling frame",
    )
    _required_columns(
        era5,
        (*key_columns, *ERA5_DERIVED_FEATURES),
        label="local-calendar ERA5 frame",
    )
    for label, frame in (("local", local), ("ERA5", era5)):
        if frame.unique(list(key_columns)).height != frame.height:
            raise RuntimeError(f"{label} modelling keys are duplicated")

    invalid_target = local.filter(
        pl.col("PM2.5").is_not_null() & ~pl.col("PM2.5").cast(pl.Float64).is_finite()
    )
    if not invalid_target.is_empty():
        raise RuntimeError("local modelling target contains a non-finite value")

    target = local.filter(pl.col("PM2.5").is_not_null())
    era5_values = era5.select(*key_columns, *ERA5_DERIVED_FEATURES).with_columns(
        pl.lit(True).alias("_era5_row_present")
    )
    joined = target.join(era5_values, on=list(key_columns), how="left").with_columns(
        _complete_expr(LOCAL_WEATHER_FEATURES).alias("_local_complete"),
        _complete_expr(ERA5_DERIVED_FEATURES).alias("_era5_complete"),
        _complete_expr(TEMPORAL_VALUE_FEATURES).alias("_temporal_complete"),
    )
    joined = joined.with_columns(
        (pl.col("_local_complete") & pl.col("_era5_complete") & pl.col("_temporal_complete")).alias(
            "_paired"
        )
    )

    coverage = (
        joined.group_by("station_name")
        .agg(
            pl.len().alias("target_rows"),
            pl.col("_era5_row_present").is_null().sum().alias("era5_join_missing"),
            (~pl.col("_local_complete")).sum().alias("local_feature_incomplete"),
            (~pl.col("_era5_complete")).sum().alias("era5_feature_incomplete"),
            (~pl.col("_temporal_complete")).sum().alias("temporal_feature_incomplete"),
            pl.col("_paired").sum().alias("paired_rows"),
        )
        .sort("station_name")
    )
    values = joined.filter(pl.col("_paired")).drop(
        "_era5_row_present",
        "_local_complete",
        "_era5_complete",
        "_temporal_complete",
        "_paired",
    )
    return PairedRows(values=values.sort(*key_columns), coverage=coverage)


def station_scope(
    *,
    candidate_stations: Iterable[str],
    target_stations: Iterable[str],
    analyzed_stations: Iterable[str],
) -> dict[str, Any]:
    candidates = set(candidate_stations)
    targets = set(target_stations)
    analyzed = set(analyzed_stations)
    if targets - candidates:
        raise RuntimeError(
            "PM2.5 target stations are absent from the reviewed ERA5 station generation: "
            f"{sorted(targets - candidates)}"
        )
    if analyzed - targets:
        raise RuntimeError(
            f"analyzed stations are absent from the PM2.5 target rows: {sorted(analyzed - targets)}"
        )

    excluded = [
        {
            "station_name": station_name,
            "reason": "no_pm25_target_rows_in_analysis_year",
        }
        for station_name in sorted(candidates - targets)
    ]
    excluded.extend(
        {
            "station_name": station_name,
            "reason": "no_common_complete_rows_for_all_feature_sets",
        }
        for station_name in sorted(targets - analyzed)
    )
    return {
        "candidate_stations": len(candidates),
        "target_available_stations": len(targets),
        "analyzed_stations": len(analyzed),
        "excluded_stations": excluded,
    }


def explicit_time_splits(
    frame: pl.DataFrame,
    folds: tuple[TimeFold, ...],
) -> tuple[Split, ...]:
    """Apply reviewed local-time boundaries without slicing through timestamps."""
    _required_columns(frame, ("ts_local",), label="ERA5 value-add frame")
    splits: list[Split] = []
    for fold in folds:
        train = frame.filter(
            pl.col("ts_local").is_between(
                fold.train_start,
                fold.train_end,
                closed="left",
            )
        ).sort("station_name", "ts_local")
        test = frame.filter(
            pl.col("ts_local").is_between(
                fold.test_start,
                fold.test_end,
                closed="left",
            )
        ).sort("station_name", "ts_local")
        if train.is_empty() or test.is_empty():
            raise RuntimeError(f"ERA5 value-add fold {fold.name} has no train or test rows")
        train_max = _datetime_scalar(
            train["ts_local"].max(),
            label=f"ERA5 value-add fold {fold.name} train maximum",
        )
        test_min = _datetime_scalar(
            test["ts_local"].min(),
            label=f"ERA5 value-add fold {fold.name} test minimum",
        )
        if train_max >= test_min:
            raise RuntimeError(f"ERA5 value-add fold {fold.name} is not strictly forward in time")
        splits.append(Split(name=fold.name, train=train, test=test))
    return tuple(splits)


def _fit_predict(
    train: pl.DataFrame,
    test: pl.DataFrame,
    features: tuple[str, ...],
    model: ModelConfig,
) -> tuple[np.ndarray, np.ndarray]:
    import lightgbm as lgb

    estimator = lgb.LGBMRegressor(
        n_estimators=model.n_estimators,
        learning_rate=model.learning_rate,
        num_leaves=model.num_leaves,
        min_child_samples=model.min_child_samples,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        random_state=model.seed,
        n_jobs=model.n_jobs,
        verbose=-1,
    )
    estimator.fit(train.select(features).to_numpy(), train["PM2.5"].to_numpy())
    prediction = np.asarray(estimator.predict(test.select(features).to_numpy()), dtype=float)
    return test["PM2.5"].to_numpy(), prediction


def evaluate_paired_models(
    frame: pl.DataFrame,
    config: Era5ValueConfig,
) -> pl.DataFrame:
    """Score four information sets per station on identical explicit-time rows."""
    _required_columns(
        frame,
        (
            "station_name",
            "ts_local",
            "PM2.5",
            *(name for features in ERA5_VALUE_FEATURE_SETS.values() for name in features),
        ),
        label="paired ERA5 value-add frame",
    )
    rows: list[dict[str, object]] = []
    for station in sorted(frame["station_name"].unique().to_list()):
        station_frame = frame.filter(pl.col("station_name") == station)
        for split in explicit_time_splits(station_frame, config.folds):
            for feature_set, features in ERA5_VALUE_FEATURE_SETS.items():
                started = perf_counter()
                truth, prediction = _fit_predict(split.train, split.test, features, config.model)
                elapsed = perf_counter() - started
                metrics = evaluate_predictions(
                    truth,
                    prediction,
                    exceedance_threshold=None,
                )
                rows.append(
                    {
                        "station_name": station,
                        "fold": split.name,
                        "feature_set": feature_set,
                        "n_train": split.train.height,
                        "n_test": split.test.height,
                        "rmse": metrics.rmse,
                        "mae": metrics.mae,
                        "r2": metrics.r2,
                        "fit_seconds": elapsed,
                    }
                )
    if not rows:
        raise RuntimeError("ERA5 value-add evaluation produced no scores")
    return pl.DataFrame(rows).sort("station_name", "fold", "feature_set")


_PAIRED_COMPARISONS: tuple[tuple[str, str, str], ...] = (
    ("combined", "local_weather", "combined_minus_local"),
    ("era5_weather", "local_weather", "era5_minus_local"),
    ("local_weather", "temporal_only", "local_minus_temporal"),
)


def paired_metric_deltas(scores: pl.DataFrame) -> pl.DataFrame:
    """Subtract paired reference metrics and keep improvement direction readable."""
    _required_columns(
        scores,
        (
            "station_name",
            "fold",
            "feature_set",
            "n_train",
            "n_test",
            "rmse",
            "mae",
            "r2",
        ),
        label="ERA5 value-add scores",
    )
    rows: list[dict[str, object]] = []
    for group in scores.partition_by("station_name", "fold", maintain_order=True):
        station = str(group["station_name"][0])
        fold = str(group["fold"][0])
        indexed = {str(row["feature_set"]): row for row in group.iter_rows(named=True)}
        if set(indexed) != set(ERA5_VALUE_FEATURE_SETS):
            raise RuntimeError(f"ERA5 value-add scores are incomplete for {station}/{fold}")
        for candidate_name, reference_name, comparison in _PAIRED_COMPARISONS:
            candidate = indexed[candidate_name]
            reference = indexed[reference_name]
            if (candidate["n_train"], candidate["n_test"]) != (
                reference["n_train"],
                reference["n_test"],
            ):
                raise RuntimeError(f"ERA5 value-add rows are not paired for {station}/{fold}")
            rmse_delta = float(candidate["rmse"]) - float(reference["rmse"])
            mae_delta = float(candidate["mae"]) - float(reference["mae"])
            r2_delta = float(candidate["r2"]) - float(reference["r2"])
            rows.append(
                {
                    "station_name": station,
                    "fold": fold,
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
    return pl.DataFrame(rows).sort("station_name", "fold", "comparison")


def summarise_metric_deltas(deltas: pl.DataFrame) -> dict[str, Any]:
    """Summarise paired changes without hiding the worst temporal fold."""
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
        fold_summary = (
            subset.group_by("fold")
            .agg(
                pl.col("rmse_delta").median().alias("median_rmse_delta"),
                pl.col("r2_delta").median().alias("median_r2_delta"),
            )
            .sort("median_rmse_delta", descending=True)
        )
        known = (
            as_int(counts["both_improved"])
            + as_int(counts["both_worse"])
            + as_int(counts["exact_tie"])
        )
        comparisons[str(comparison)] = {
            "station_folds": subset.height,
            "stations": subset["station_name"].n_unique(),
            "median_rmse_delta": as_float(subset["rmse_delta"].median()),
            "rmse_delta_q25": as_float(subset["rmse_delta"].quantile(0.25)),
            "rmse_delta_q75": as_float(subset["rmse_delta"].quantile(0.75)),
            "median_mae_delta": as_float(subset["mae_delta"].median()),
            "median_r2_delta": as_float(subset["r2_delta"].median()),
            "r2_delta_q25": as_float(subset["r2_delta"].quantile(0.25)),
            "r2_delta_q75": as_float(subset["r2_delta"].quantile(0.75)),
            "both_improved": as_int(counts["both_improved"]),
            "both_worse": as_int(counts["both_worse"]),
            "exact_tie": as_int(counts["exact_tie"]),
            "mixed": subset.height - known,
            "worst_fold_by_median_rmse_delta": str(fold_summary["fold"][0]),
            "folds": fold_summary.to_dicts(),
        }
    return {"comparisons": comparisons}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_era5_value_result(
    result: Era5ValueResult,
    *,
    destination: Path | None = None,
) -> dict[str, Path]:
    """Write a complete result with an atomic directory replacement."""
    out = destination or outputs_dir("m8_era5_value")
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
        "summary": out / "summary.json",
        "manifest": out / "manifest.json",
    }


def _observation_inputs(data_root: Path, *, year: int) -> tuple[InputFile, ...]:
    root = data_root / "processed" / "observations" / f"year={year}"
    paths = tuple(sorted(root.rglob("*.parquet"))) if root.is_dir() else ()
    if not paths:
        raise FileNotFoundError(f"canonical observation year not found: {root}")
    return tuple(_input_file(path) for path in paths)


def _manifest_input_files(files: tuple[InputFile, ...], *, data_root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": item.path.relative_to(data_root).as_posix(),
            "bytes": item.bytes,
            "sha256": item.sha256,
        }
        for item in files
    ]


def run_era5_value(
    *,
    data_root: Path,
    generation_sha256: str,
    pilot: bool,
    config: Era5ValueConfig | None = None,
    generated_at: str | None = None,
) -> Era5ValueResult:
    """Load, pair, and score one immutable ERA5 station generation."""
    selected = config or load_era5_value_config()
    observation_before = _observation_inputs(data_root, year=selected.year)
    era5 = load_local_era5_year(
        data_root,
        year=selected.year,
        generation_sha256=generation_sha256,
    )
    stations = list(selected.pilot_stations) if pilot else None
    local = build_modelling_frame(
        data_root / "processed" / "observations",
        period=(selected.year, selected.year),
        stations=stations,
    )
    era5_values = (
        era5.values.filter(pl.col("station_name").is_in(stations))
        if stations is not None
        else era5.values
    )
    paired = prepare_paired_rows(local, era5_values)
    scores = evaluate_paired_models(paired.values, selected)
    deltas = paired_metric_deltas(scores)
    summary = summarise_metric_deltas(deltas)

    all_before = (*observation_before, *era5.input_files)
    all_after = tuple(_input_file(item.path) for item in all_before)
    if all_before != all_after:
        raise RuntimeError("an analysis input changed while models were fitting")

    run_at = generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    git_sha, git_dirty = git_state()
    candidate_station_names = (
        selected.pilot_stations
        if stations is not None
        else tuple(era5_values["station_name"].unique().to_list())
    )
    station_scope_manifest = station_scope(
        candidate_stations=candidate_station_names,
        target_stations=local.filter(pl.col("PM2.5").is_not_null())["station_name"]
        .unique()
        .to_list(),
        analyzed_stations=paired.values["station_name"].unique().to_list(),
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "complete": True,
        "generated_at": run_at,
        "year": selected.year,
        "mode": "pilot" if pilot else "full",
        "stations": sorted(paired.values["station_name"].unique().to_list()),
        "station_scope": station_scope_manifest,
        "inventory_generation_sha256": era5.inventory_generation_sha256,
        "feature_sets": {
            name: list(features) for name, features in ERA5_VALUE_FEATURE_SETS.items()
        },
        "folds": [
            {
                key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in asdict(fold).items()
            }
            for fold in selected.folds
        ],
        "model": asdict(selected.model),
        "input_files": _manifest_input_files(all_before, data_root=data_root),
        "paired_rows": paired.values.height,
        "score_rows": scores.height,
        "delta_rows": deltas.height,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
    }
    return Era5ValueResult(
        scores=scores,
        deltas=deltas,
        coverage=paired.coverage,
        summary=summary,
        manifest=manifest,
    )
