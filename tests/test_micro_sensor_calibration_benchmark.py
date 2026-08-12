"""The January benchmark preserves source states and uses grouped transfer folds."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from twair.analysis import micro_sensor_calibration as readiness
from twair.analysis import micro_sensor_calibration_benchmark as benchmark
from twair.analysis.era5_value import ModelConfig
from twair.config import ConfigError

READINESS_ID = "1f76ea400995080027701f80c311438fab3e6d823f5665681b9ca79a4aad81fd"


def _model() -> ModelConfig:
    return ModelConfig(
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=10,
        n_jobs=1,
        seed=20260812,
    )


def _config() -> benchmark.MicroSensorBenchmarkConfig:
    return benchmark.MicroSensorBenchmarkConfig(
        readiness_generation_sha256=READINESS_ID,
        date_folds=25,
        station_folds=10,
        feature_sets={
            "raw_micro": (),
            "micro_only": ("pm25_mean",),
            "micro_weather": ("pm25_mean", "humidity_mean", "temperature_mean"),
        },
        model=_model(),
    )


def _raw_config() -> dict[str, Any]:
    return {
        "analysis": {
            "readiness_generation_sha256": READINESS_ID,
            "date_folds": 25,
            "station_folds": 10,
            "feature_sets": {
                "raw_micro": [],
                "micro_only": ["pm25_mean"],
                "micro_weather": ["pm25_mean", "humidity_mean", "temperature_mean"],
            },
            "model": {
                "n_estimators": 200,
                "learning_rate": 0.05,
                "num_leaves": 31,
                "min_child_samples": 10,
                "n_jobs": 1,
                "seed": 20260812,
            },
        }
    }


def _hourly_rows() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    start = datetime(2025, 1, 1)
    stations = [f"reference-{index:02d}" for index in range(10)]
    for day in range(25):
        for station_index, station in enumerate(stations):
            for device_index in range(2):
                value = float(8 + day + station_index + device_index)
                rows.append(
                    {
                        "device_id": f"device-{station_index:02d}-{device_index}",
                        "hour": start + timedelta(days=day, hours=device_index),
                        "pm25_source_rows": 60,
                        "pm25_distinct_timestamps": 60,
                        "pm25_mean": value,
                        "pm25_extreme_source_rows": 0,
                        "humidity_source_rows": 60,
                        "humidity_distinct_timestamps": 60,
                        "humidity_mean": float(50 + device_index),
                        "humidity_extreme_source_rows": 0,
                        "temperature_source_rows": 60,
                        "temperature_distinct_timestamps": 60,
                        "temperature_mean": float(20 + day / 10),
                        "temperature_extreme_source_rows": 0,
                        "station_name": station,
                        "distance_km": float(0.1 + station_index / 100),
                        "ground_row_present": True,
                        "ground_flag": "valid",
                        "ground_pm25": value + 1.5,
                        "ground_eligible": True,
                        "eligibility_reason": "eligible",
                    }
                )
    rows.append(
        {
            **rows[0],
            "device_id": "withheld-device",
            "hour": start + timedelta(hours=3),
            "humidity_mean": None,
            "eligibility_reason": "micro_humidity_below_coverage",
        }
    )
    return pl.DataFrame(rows, schema=readiness._RESULT_SCHEMAS["hourly_pairs"])


def _empty_member(name: str) -> pl.DataFrame:
    return pl.DataFrame(schema=readiness._RESULT_SCHEMAS[name])


def _write_readiness_generation(
    root: Path,
    *,
    hourly_pairs: pl.DataFrame | None = None,
) -> tuple[str, Path]:
    members = {
        name: hourly_pairs
        if name == "hourly_pairs" and hourly_pairs is not None
        else _empty_member(name)
        for name in readiness._RESULT_SCHEMAS
    }
    if hourly_pairs is None:
        members["hourly_pairs"] = _hourly_rows()
    rows = {name: frame.height for name, frame in members.items()}
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "analysis": "micro_sensor_calibration_readiness",
        "complete": True,
        "panel_dates": 25,
        "config": {"primary_distance_km": 1.0, "primary_minimum_rows": 45},
        "config_sha256": readiness._hash_value(
            {"primary_distance_km": 1.0, "primary_minimum_rows": 45}
        ),
        "inputs": {"fixture": "reviewed-real-shape"},
        "input_files": [],
        "output_rows": rows,
        "claim_boundary": dict(readiness._CLAIM_BOUNDARY),
    }
    identity_payload = {
        key: manifest[key]
        for key in (
            "schema_version",
            "analysis",
            "config_sha256",
            "inputs",
            "input_files",
            "output_rows",
            "claim_boundary",
        )
    }
    generation = readiness._hash_value(identity_payload)
    manifest["output_identity_sha256"] = generation
    directory = root / "outputs" / "micro_sensor_calibration_readiness" / "generations" / generation
    directory.mkdir(parents=True)
    for name, frame in members.items():
        frame.write_parquet(directory / f"{name}.parquet")
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    (directory / "summary.json").write_text(
        json.dumps({"output_rows": rows}, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    return generation, directory


def _geography() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": [f"reference-{index:02d}" for index in range(10)],
            "airzone_official": [f"zone-{index % 3}" for index in range(10)],
        }
    )


def _prepared(
    tmp_path: Path,
) -> tuple[benchmark.PreparedMicroSensorBenchmark, benchmark.MicroSensorBenchmarkConfig]:
    generation, _ = _write_readiness_generation(tmp_path)
    loaded = benchmark.load_micro_sensor_readiness_rows(
        data_root=tmp_path, generation_sha256=generation
    )
    config = replace(_config(), readiness_generation_sha256=generation)
    return benchmark.prepare_micro_sensor_benchmark(loaded, _geography(), config), config


def _observed_fit(
    train: pl.DataFrame,
    test: pl.DataFrame,
    features: tuple[str, ...],
    model: ModelConfig,
) -> np.ndarray:
    assert model.n_jobs == 1
    assert not set(train.select("device_id", "hour").iter_rows()) & set(
        test.select("device_id", "hour").iter_rows()
    )
    assert not set(train["date"].to_list()) & set(test["date"].to_list()) or not set(
        train["station_name"].to_list()
    ) & set(test["station_name"].to_list())
    truth = test["ground_pm25"].to_numpy()
    if features == ("pm25_mean",):
        return truth
    assert features == ("pm25_mean", "humidity_mean", "temperature_mean")
    return truth + 0.5


def test_the_shipped_benchmark_config_pins_the_generation_folds_features_and_serial_model() -> None:
    config = benchmark.load_micro_sensor_benchmark_config()

    assert config == _config()
    assert config.model.n_jobs == 1
    assert config.feature_sets == benchmark.BENCHMARK_FEATURE_SETS


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw["analysis"].update({"unknown": 1}), "unknown"),
        (lambda raw: raw["analysis"].update({"date_folds": True}), "date_folds"),
        (lambda raw: raw["analysis"].update({"station_folds": 9}), "station_folds"),
        (
            lambda raw: raw["analysis"]["feature_sets"].update({"raw_micro": ["pm25_mean"]}),
            "feature_sets",
        ),
        (lambda raw: raw["analysis"]["model"].update({"n_jobs": 2}), "n_jobs=1"),
        (lambda raw: raw["analysis"]["model"].update({"seed": True}), "seed"),
    ],
)
def test_the_benchmark_config_rejects_unknown_or_changed_contracts(
    mutation: Any,
    message: str,
) -> None:
    raw = _raw_config()
    mutation(raw)

    with pytest.raises(ConfigError, match=message):
        benchmark.load_micro_sensor_benchmark_config(raw)


def test_readiness_loading_validates_every_member_and_keeps_noneligible_source_rows(
    tmp_path: Path,
) -> None:
    generation, directory = _write_readiness_generation(tmp_path)

    loaded = benchmark.load_micro_sensor_readiness_rows(
        data_root=tmp_path, generation_sha256=generation
    )

    assert loaded.rows.height == 501
    assert loaded.rows.filter(pl.col("eligibility_reason") != "eligible").height == 1
    assert {item.path.name for item in loaded.input_files} == {
        "manifest.json",
        "summary.json",
        *(f"{name}.parquet" for name in readiness._RESULT_SCHEMAS),
    }
    (directory / "coverage.parquet").unlink()
    with pytest.raises(RuntimeError, match="coverage"):
        benchmark.load_micro_sensor_readiness_rows(data_root=tmp_path, generation_sha256=generation)


def test_readiness_loading_rejects_changed_claims_and_table_schemas(tmp_path: Path) -> None:
    generation, directory = _write_readiness_generation(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["claim_boundary"]["calibration_fitted"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="manifest contract"):
        benchmark.load_micro_sensor_readiness_rows(data_root=tmp_path, generation_sha256=generation)

    second_root = tmp_path / "schema"
    second_generation, second_directory = _write_readiness_generation(second_root)
    malformed = pl.read_parquet(second_directory / "hourly_pairs.parquet").drop("ground_flag")
    malformed.write_parquet(second_directory / "hourly_pairs.parquet")
    with pytest.raises(RuntimeError, match="hourly_pairs schema"):
        benchmark.load_micro_sensor_readiness_rows(
            data_root=second_root, generation_sha256=second_generation
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda frame: pl.concat([frame, frame.head(1)]), "duplicated"),
        (
            lambda frame: frame.with_columns(
                pl.when(pl.int_range(pl.len()) == 0)
                .then(None)
                .otherwise(pl.col("ground_pm25"))
                .alias("ground_pm25")
            ),
            "eligible values",
        ),
        (
            lambda frame: frame.with_columns(
                pl.when(pl.int_range(pl.len()) == 0)
                .then(float("inf"))
                .otherwise(pl.col("humidity_mean"))
                .alias("humidity_mean")
            ),
            "eligible values",
        ),
    ],
)
def test_invalid_readiness_rows_are_rejected_instead_of_repaired(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    generation, _ = _write_readiness_generation(tmp_path, hourly_pairs=mutation(_hourly_rows()))
    loaded = benchmark.load_micro_sensor_readiness_rows(
        data_root=tmp_path, generation_sha256=generation
    )

    with pytest.raises(RuntimeError, match=message):
        benchmark.prepare_micro_sensor_benchmark(
            loaded, _geography(), replace(_config(), readiness_generation_sha256=generation)
        )


def test_preparation_assigns_each_date_and_station_once_without_leakage(tmp_path: Path) -> None:
    generation, _ = _write_readiness_generation(tmp_path)
    loaded = benchmark.load_micro_sensor_readiness_rows(
        data_root=tmp_path, generation_sha256=generation
    )

    prepared = benchmark.prepare_micro_sensor_benchmark(
        loaded,
        _geography(),
        replace(_config(), readiness_generation_sha256=generation),
    )

    assert prepared.rows.height == 500
    assert prepared.source_rows == 501
    assert prepared.rows["eligibility_reason"].unique().to_list() == ["eligible"]
    dates = prepared.fold_membership.filter(pl.col("fold_kind") == "held_date")
    stations = prepared.fold_membership.filter(pl.col("fold_kind") == "held_station")
    assert dates.height == 25
    assert dates["group"].n_unique() == 25
    assert stations.height == 10
    assert stations["group"].n_unique() == 10
    assert sorted(stations["fold_index"].unique().to_list()) == list(range(10))
    assert prepared.rows["station_fold"].null_count() == 0
    assert prepared.rows["date"].n_unique() == 25


@pytest.mark.parametrize(
    ("geography", "message"),
    [
        (_geography().head(9), "absent"),
        (pl.concat([_geography(), _geography().head(1)]), "duplicated"),
    ],
)
def test_station_metadata_must_cover_each_reference_station_once(
    tmp_path: Path,
    geography: pl.DataFrame,
    message: str,
) -> None:
    generation, _ = _write_readiness_generation(tmp_path)
    loaded = benchmark.load_micro_sensor_readiness_rows(
        data_root=tmp_path, generation_sha256=generation
    )

    with pytest.raises(RuntimeError, match=message):
        benchmark.prepare_micro_sensor_benchmark(
            loaded,
            geography,
            replace(_config(), readiness_generation_sha256=generation),
        )


def test_grouped_evaluation_fits_two_serial_models_and_tests_each_row_once_per_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, config = _prepared(tmp_path)
    calls: list[tuple[str, ...]] = []

    def observed_fit(
        train: pl.DataFrame,
        test: pl.DataFrame,
        features: tuple[str, ...],
        model: ModelConfig,
    ) -> np.ndarray:
        calls.append(features)
        return _observed_fit(train, test, features, model)

    monkeypatch.setattr(benchmark, "_fit_predict", observed_fit)
    evaluation = benchmark.evaluate_micro_sensor_benchmark(prepared, config)

    assert evaluation.folds.height == 35
    assert evaluation.predictions.height == prepared.rows.height * 2
    assert len(calls) == 35 * 2
    assert calls.count(("pm25_mean",)) == 35
    assert calls.count(("pm25_mean", "humidity_mean", "temperature_mean")) == 35
    assert evaluation.predictions["raw_micro"].equals(
        evaluation.predictions["pm25_mean"].rename("raw_micro")
    )
    tested = evaluation.predictions.group_by("device_id", "hour").len()
    assert tested["len"].unique().to_list() == [2]
    assert evaluation.folds["train_membership_sha256"].str.len_chars().unique().to_list() == [64]
    assert evaluation.folds["test_membership_sha256"].str.len_chars().unique().to_list() == [64]


def _unequal_density_predictions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "evaluation": ["held_date"] * 4,
            "fold": ["held_date_2025-01-01"] * 4,
            "fold_index": [0] * 4,
            "device_id": ["a1", "a2", "a3", "b1"],
            "hour": [datetime(2025, 1, 1)] * 4,
            "station_name": ["A", "A", "A", "B"],
            "distance_km": [0.1, 0.2, 0.3, 0.1],
            "pm25_mean": [5.0, 5.0, 5.0, 30.0],
            "truth": [10.0, 10.0, 10.0, 20.0],
            "raw_micro": [5.0, 5.0, 5.0, 30.0],
            "micro_only": [9.0, 9.0, 9.0, 21.0],
            "micro_weather": [10.0, 10.0, 10.0, 20.0],
        },
        schema_overrides={"fold_index": pl.Int64},
    )


def test_metrics_preserve_device_and_reference_station_weighting_and_paired_deltas() -> None:
    predictions = _unequal_density_predictions()

    scores = benchmark.score_micro_sensor_predictions(predictions)
    deltas = benchmark.micro_sensor_benchmark_deltas(scores)

    assert scores.height == 2 * 3
    raw = scores.filter(pl.col("feature_set") == "raw_micro")
    assert (
        raw.filter(pl.col("evaluation_unit") == "device_hour")["rmse"].item()
        != raw.filter(pl.col("evaluation_unit") == "reference_station_hour")["rmse"].item()
    )
    assert scores.group_by("evaluation_unit").agg(pl.col("n").n_unique())["n"].to_list() == [1, 1]
    weather = scores.filter(pl.col("feature_set") == "micro_weather")
    assert weather["rmse"].to_list() == [0.0, 0.0]
    weather_minus_raw = deltas.filter(pl.col("comparison") == "micro_weather_minus_raw_micro")
    assert weather_minus_raw["rmse_improved"].all()
    assert weather_minus_raw["mae_improved"].all()
    assert weather_minus_raw["abs_bias_improved"].all()


def test_nonfinite_predictions_and_constant_targets_fail_instead_of_disappearing() -> None:
    predictions = _unequal_density_predictions()
    nonfinite = predictions.with_columns(
        pl.when(pl.col("device_id") == "a1")
        .then(float("nan"))
        .otherwise(pl.col("micro_only"))
        .alias("micro_only")
    )
    with pytest.raises(RuntimeError, match="non-finite"):
        benchmark.score_micro_sensor_predictions(nonfinite)

    constant = predictions.with_columns(pl.lit(10.0).alias("truth"))
    with pytest.raises(RuntimeError, match="constant target"):
        benchmark.score_micro_sensor_predictions(constant)

    conflicting = predictions.with_columns(
        pl.when(pl.col("device_id") == "a1").then(11.0).otherwise(pl.col("truth")).alias("truth")
    )
    with pytest.raises(RuntimeError, match="ground targets disagree"):
        benchmark.score_micro_sensor_predictions(conflicting)


def test_an_input_member_change_during_serial_fits_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation, directory = _write_readiness_generation(tmp_path)
    config = replace(_config(), readiness_generation_sha256=generation)
    calls = 0

    def mutating_fit(
        train: pl.DataFrame,
        test: pl.DataFrame,
        features: tuple[str, ...],
        model: ModelConfig,
    ) -> np.ndarray:
        nonlocal calls
        calls += 1
        if calls == 1:
            summary_path = directory / "summary.json"
            summary_path.write_bytes(summary_path.read_bytes() + b" ")
        return _observed_fit(train, test, features, model)

    monkeypatch.setattr(benchmark, "_fit_predict", mutating_fit)
    with pytest.raises(RuntimeError, match="input changed during"):
        benchmark.run_micro_sensor_calibration_benchmark(
            data_root=tmp_path,
            config=config,
            geography=_geography(),
        )


def test_the_runner_and_writer_bind_inputs_and_publish_only_seven_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation, _ = _write_readiness_generation(tmp_path)
    config = replace(_config(), readiness_generation_sha256=generation)
    monkeypatch.setattr(benchmark, "_fit_predict", _observed_fit)
    result = benchmark.run_micro_sensor_calibration_benchmark(
        data_root=tmp_path,
        config=config,
        geography=_geography(),
        generated_at="2026-08-12T00:00:00+00:00",
        git_sha="b" * 40,
        git_dirty=False,
    )
    destination = tmp_path / "published"

    written = benchmark.write_micro_sensor_calibration_benchmark_result(
        result, destination=destination
    )

    assert set(written) == {
        "fold_membership",
        "folds",
        "predictions",
        "scores",
        "deltas",
        "summary",
        "manifest",
    }
    assert {path.name for path in destination.iterdir()} == {
        "fold_membership.parquet",
        "folds.parquet",
        "predictions.parquet",
        "scores.parquet",
        "deltas.parquet",
        "summary.json",
        "manifest.json",
    }
    persisted = json.loads(written["manifest"].read_text(encoding="utf-8"))
    assert persisted["claim_boundary"] == benchmark.CLAIM_BOUNDARY
    assert persisted["readiness_generation_sha256"] == generation
    assert all(not Path(item["path"]).is_absolute() for item in persisted["inputs"])
    assert not list(tmp_path.glob(".published.staging-*"))
    assert not list(tmp_path.glob(".published.backup-*"))

    repeated = benchmark.write_micro_sensor_calibration_benchmark_result(
        result, destination=destination
    )
    assert repeated == written
    assert not list(tmp_path.glob(".published.staging-*"))
    assert not list(tmp_path.glob(".published.backup-*"))

    malformed = replace(result, predictions=result.predictions.drop("micro_weather"))
    with pytest.raises(RuntimeError, match="predictions schema"):
        benchmark.write_micro_sensor_calibration_benchmark_result(
            malformed, destination=tmp_path / "malformed"
        )


@pytest.mark.parametrize("interrupt_case", ["first_after", "second_before", "second_after"])
def test_an_interrupt_during_either_directory_rename_restores_the_previous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_case: str,
) -> None:
    generation, _ = _write_readiness_generation(tmp_path)
    config = replace(_config(), readiness_generation_sha256=generation)
    monkeypatch.setattr(benchmark, "_fit_predict", _observed_fit)
    result = benchmark.run_micro_sensor_calibration_benchmark(
        data_root=tmp_path,
        config=config,
        geography=_geography(),
    )
    destination = tmp_path / "published"
    destination.mkdir()
    baseline = destination / "baseline.txt"
    baseline.write_text("complete", encoding="utf-8")
    real_replace = Path.replace
    calls = 0

    def interrupted_replace(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if (calls, interrupt_case) == (1, "first_after"):
            real_replace(path, target)
            raise KeyboardInterrupt
        if calls == 2:
            if interrupt_case == "second_after":
                real_replace(path, target)
                raise KeyboardInterrupt
            if interrupt_case == "second_before":
                raise KeyboardInterrupt
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt):
        benchmark.write_micro_sensor_calibration_benchmark_result(result, destination=destination)

    assert baseline.read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".published.staging-*"))
    assert not list(tmp_path.glob(".published.backup-*"))
