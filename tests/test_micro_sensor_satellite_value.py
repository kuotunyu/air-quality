"""Satellite context is tested on one explicit cohort without being called fusion."""

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
from twair.analysis import micro_sensor_satellite_value as satellite_value
from twair.config import ConfigError
from twair.net import sha256_file


def _raw_config(
    readiness_generation: str = "a" * 64,
    benchmark_generation: str = "b" * 64,
) -> dict[str, Any]:
    return {
        "analysis": {
            "readiness_generation_sha256": readiness_generation,
            "benchmark_generation_sha256": benchmark_generation,
            "input_sha256": {
                "readiness_manifest": "c" * 64,
                "hourly_pairs": "d" * 64,
                "satellite_context": "e" * 64,
                "benchmark_manifest": "f" * 64,
                "fold_membership": "1" * 64,
            },
            "date_folds": 25,
            "station_folds": 10,
            "satellite_sources": ["maiac_aod", "s5p_no2", "s5p_so2"],
            "feature_sets": {
                "raw_micro": [],
                "micro_only": ["pm25_mean"],
                "micro_weather": ["pm25_mean", "humidity_mean", "temperature_mean"],
                "micro_satellite": ["pm25_mean", "maiac_aod", "s5p_no2", "s5p_so2"],
                "micro_weather_satellite": [
                    "pm25_mean",
                    "humidity_mean",
                    "temperature_mean",
                    "maiac_aod",
                    "s5p_no2",
                    "s5p_so2",
                ],
            },
            "comparisons": [
                {
                    "candidate": candidate,
                    "reference": reference,
                    "name": name,
                }
                for candidate, reference, name in satellite_value.COMPARISONS
            ],
            "model": {
                "n_estimators": 200,
                "learning_rate": 0.05,
                "num_leaves": 31,
                "min_child_samples": 10,
                "subsample": 0.8,
                "subsample_freq": 1,
                "colsample_bytree": 0.8,
                "n_jobs": 1,
                "seed": 20260812,
            },
        }
    }


def _hourly_rows() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    start = datetime(2025, 1, 1)
    for day in range(25):
        for station_index in range(11):
            station = f"reference-{station_index:02d}"
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


def _satellite_rows() -> pl.DataFrame:
    units = {"maiac_aod": "1", "s5p_no2": "mol/m2", "s5p_so2": "mol/m2"}
    rows: list[dict[str, object]] = []
    for station_index in range(11):
        for source_index, source in enumerate(satellite_value.SATELLITE_SOURCES):
            observed = not (station_index == 10 and source == "maiac_aod")
            rows.append(
                {
                    "station_name": f"reference-{station_index:02d}",
                    "source": source,
                    "satellite_value": float(station_index + source_index / 10)
                    if observed
                    else None,
                    "satellite_observed": observed,
                    "pair_observed": observed,
                    "satellite_unit": units[source],
                    "collection_id": f"fixture/{source}",
                    "band": source,
                    "sample_scale_m": 1000,
                    "linked_devices": 2,
                }
            )
    return pl.DataFrame(rows, schema=readiness._RESULT_SCHEMAS["satellite_context"])


def _fold_membership() -> pl.DataFrame:
    start = datetime(2025, 1, 1)
    date_rows = [
        {
            "fold_kind": "held_date",
            "group": (start + timedelta(days=index)).date().isoformat(),
            "fold": f"held_date_{(start + timedelta(days=index)).date().isoformat()}",
            "fold_index": index,
            "airzone_official": None,
        }
        for index in range(25)
    ]
    station_rows = [
        {
            "fold_kind": "held_station",
            "group": f"reference-{index:02d}",
            "fold": f"held_station_{index:02d}",
            "fold_index": index,
            "airzone_official": f"zone-{index % 3}",
        }
        for index in range(11)
    ]
    station_rows[-1]["fold"] = "held_station_09"
    station_rows[-1]["fold_index"] = 9
    return pl.DataFrame([*date_rows, *station_rows], schema=benchmark._FOLD_MEMBERSHIP_SCHEMA)


def _write_generations(
    root: Path,
    *,
    hourly_pairs: pl.DataFrame | None = None,
    satellite_context: pl.DataFrame | None = None,
    fold_membership: pl.DataFrame | None = None,
) -> tuple[str, str, dict[str, Path]]:
    hourly = hourly_pairs if hourly_pairs is not None else _hourly_rows()
    satellite = satellite_context if satellite_context is not None else _satellite_rows()
    readiness_rows = {
        name: (
            hourly
            if name == "hourly_pairs"
            else satellite
            if name == "satellite_context"
            else pl.DataFrame(schema=schema)
        )
        for name, schema in readiness._RESULT_SCHEMAS.items()
    }
    output_rows = {name: frame.height for name, frame in readiness_rows.items()}
    readiness_manifest: dict[str, Any] = {
        "schema_version": 1,
        "analysis": "micro_sensor_calibration_readiness",
        "complete": True,
        "panel_dates": 25,
        "config": {"fixture": "real-shape"},
        "config_sha256": readiness._hash_value({"fixture": "real-shape"}),
        "inputs": {"fixture": "immutable"},
        "input_files": [],
        "output_rows": output_rows,
        "claim_boundary": dict(readiness._CLAIM_BOUNDARY),
    }
    readiness_identity = readiness._hash_value(
        {
            key: readiness_manifest[key]
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
    )
    readiness_manifest["output_identity_sha256"] = readiness_identity
    readiness_dir = (
        root / "outputs" / "micro_sensor_calibration_readiness" / "generations" / readiness_identity
    )
    readiness_dir.mkdir(parents=True)
    hourly.write_parquet(readiness_dir / "hourly_pairs.parquet")
    satellite.write_parquet(readiness_dir / "satellite_context.parquet")
    (readiness_dir / "manifest.json").write_text(
        json.dumps(readiness_manifest, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )

    membership = fold_membership if fold_membership is not None else _fold_membership()
    benchmark_output_rows = {
        "fold_membership": membership.height,
        "folds": 35,
        "predictions": 1000,
        "scores": 210,
        "deltas": 210,
    }
    benchmark_config = {"fixture": "grouped-folds"}
    benchmark_manifest: dict[str, Any] = {
        "schema_version": 1,
        "analysis": "micro_sensor_calibration_benchmark",
        "complete": True,
        "readiness_generation_sha256": readiness_identity,
        "config": benchmark_config,
        "config_sha256": benchmark._hash_json(benchmark_config),
        "inputs": [],
        "reviewed_station_airzone_sha256": "c" * 64,
        "output_rows": benchmark_output_rows,
        "claim_boundary": dict(benchmark.CLAIM_BOUNDARY),
        "git_sha": "d" * 40,
        "git_dirty": False,
    }
    benchmark_identity = benchmark._hash_json(
        {
            key: benchmark_manifest[key]
            for key in (
                "schema_version",
                "analysis",
                "readiness_generation_sha256",
                "config_sha256",
                "inputs",
                "reviewed_station_airzone_sha256",
                "output_rows",
                "claim_boundary",
                "git_sha",
                "git_dirty",
            )
        }
    )
    benchmark_manifest["output_identity_sha256"] = benchmark_identity
    benchmark_dir = (
        root / "outputs" / "micro_sensor_calibration_benchmark" / "generations" / benchmark_identity
    )
    benchmark_dir.mkdir(parents=True)
    membership.write_parquet(benchmark_dir / "fold_membership.parquet")
    (benchmark_dir / "manifest.json").write_text(
        json.dumps(benchmark_manifest, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    return (
        readiness_identity,
        benchmark_identity,
        {
            "readiness_manifest": readiness_dir / "manifest.json",
            "hourly_pairs": readiness_dir / "hourly_pairs.parquet",
            "satellite_context": readiness_dir / "satellite_context.parquet",
            "benchmark_manifest": benchmark_dir / "manifest.json",
            "fold_membership": benchmark_dir / "fold_membership.parquet",
        },
    )


def _config(
    readiness_generation: str,
    benchmark_generation: str,
    paths: dict[str, Path],
) -> satellite_value.MicroSensorSatelliteValueConfig:
    raw = _raw_config(readiness_generation, benchmark_generation)
    raw["analysis"]["input_sha256"] = {name: sha256_file(path) for name, path in paths.items()}
    return satellite_value.load_micro_sensor_satellite_value_config(raw)


def _prepared(
    tmp_path: Path,
) -> tuple[
    satellite_value.PreparedMicroSensorSatelliteValue,
    satellite_value.MicroSensorSatelliteValueConfig,
    dict[str, Path],
]:
    readiness_generation, benchmark_generation, paths = _write_generations(tmp_path)
    config = _config(readiness_generation, benchmark_generation, paths)
    loaded = satellite_value.load_micro_sensor_satellite_inputs(data_root=tmp_path, config=config)
    return satellite_value.prepare_micro_sensor_satellite_value(loaded, config), config, paths


def _observed_fit(
    train: pl.DataFrame,
    test: pl.DataFrame,
    features: tuple[str, ...],
    model: satellite_value.SatelliteContextModelConfig,
) -> np.ndarray:
    assert model.n_jobs == 1
    assert not set(train.select("device_id", "hour").iter_rows()) & set(
        test.select("device_id", "hour").iter_rows()
    )
    offsets = {
        ("pm25_mean",): 0.0,
        ("pm25_mean", "humidity_mean", "temperature_mean"): 0.5,
        ("pm25_mean", "maiac_aod", "s5p_no2", "s5p_so2"): 1.0,
        (
            "pm25_mean",
            "humidity_mean",
            "temperature_mean",
            "maiac_aod",
            "s5p_no2",
            "s5p_so2",
        ): 1.5,
    }
    return np.asarray(test["ground_pm25"].to_numpy(), dtype=float) + offsets[features]


def test_the_shipped_config_pins_both_generations_features_comparisons_and_serial_model() -> None:
    config = satellite_value.load_micro_sensor_satellite_value_config()

    assert config.readiness_generation_sha256 == (
        "1f76ea400995080027701f80c311438fab3e6d823f5665681b9ca79a4aad81fd"
    )
    assert config.benchmark_generation_sha256 == (
        "25cc89fdb57d1e64754edd5c3a7bbb140cad88e5e178137875dafae2103f0cc6"
    )
    assert config.feature_sets == satellite_value.FEATURE_SETS
    assert config.comparisons == satellite_value.COMPARISONS
    assert config.model.n_jobs == 1
    assert config.input_sha256 == {
        "readiness_manifest": "089f92e9b844796ea43a8ad0fd5f14d40ac3089bd4ebfa75dfb4fc783bc3b120",
        "hourly_pairs": "48638dd4e6bfe294e2857f7a3a826c449b2c10cc4cf6c4d18b88e7b1e0e4d215",
        "satellite_context": "bed41c137e3cbcb1fbb8b22d1b53d589219c2f236e349e9eede4de0f36540b87",
        "benchmark_manifest": "d1a8010b0ed33e5ac3229709a1cd1ff49e4e95e8f8e92c0d6055a864f4cf1a3f",
        "fold_membership": "312b502717452b26f302d6d25cb1ee5c60f11144e9df051e76a79b909f62e3a7",
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw["analysis"].update({"unknown": 1}), "unknown"),
        (lambda raw: raw["analysis"].update({"date_folds": 24}), "date_folds"),
        (lambda raw: raw["analysis"].update({"station_folds": True}), "station_folds"),
        (
            lambda raw: raw["analysis"].update({"satellite_sources": ["maiac_aod"]}),
            "satellite_sources",
        ),
        (
            lambda raw: raw["analysis"]["feature_sets"].update({"micro_satellite": ["pm25_mean"]}),
            "feature_sets",
        ),
        (
            lambda raw: raw["analysis"]["comparisons"].pop(),
            "comparisons",
        ),
        (lambda raw: raw["analysis"]["model"].update({"n_jobs": 2}), "n_jobs=1"),
        (lambda raw: raw["analysis"]["model"].update({"seed": True}), "seed"),
    ],
)
def test_changed_or_ambiguous_config_is_rejected(
    mutation: Any,
    message: str,
) -> None:
    raw = _raw_config()
    mutation(raw)

    with pytest.raises(ConfigError, match=message):
        satellite_value.load_micro_sensor_satellite_value_config(raw)


def test_input_loading_binds_both_generations_and_all_five_consumed_files(
    tmp_path: Path,
) -> None:
    readiness_generation, benchmark_generation, paths = _write_generations(tmp_path)

    loaded = satellite_value.load_micro_sensor_satellite_inputs(
        data_root=tmp_path,
        config=_config(readiness_generation, benchmark_generation, paths),
    )

    assert loaded.hourly_pairs.height == 551
    assert loaded.satellite_context.height == 33
    assert loaded.fold_membership.height == 36
    assert {item.path for item in loaded.input_files} == set(paths.values())
    changed_hashes = dict(_config(readiness_generation, benchmark_generation, paths).input_sha256)
    changed_hashes["fold_membership"] = "0" * 64
    with pytest.raises(RuntimeError, match="immutable input identity"):
        satellite_value.load_micro_sensor_satellite_inputs(
            data_root=tmp_path,
            config=replace(
                _config(readiness_generation, benchmark_generation, paths),
                input_sha256=changed_hashes,
            ),
        )
    manifest = json.loads(paths["benchmark_manifest"].read_text(encoding="utf-8"))
    manifest["readiness_generation_sha256"] = "f" * 64
    paths["benchmark_manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="readiness generation"):
        satellite_value.load_micro_sensor_satellite_inputs(
            data_root=tmp_path,
            config=_config(readiness_generation, benchmark_generation, paths),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda frame: frame.with_columns(
                pl.when(
                    (pl.col("station_name") == "reference-00") & (pl.col("source") == "maiac_aod")
                )
                .then(None)
                .otherwise(pl.col("satellite_observed"))
                .alias("satellite_observed")
            ),
            "observation flags",
        ),
        (
            lambda frame: frame.with_columns(
                pl.when(
                    (pl.col("station_name") == "reference-00") & (pl.col("source") == "maiac_aod")
                )
                .then(float("inf"))
                .otherwise(pl.col("satellite_value"))
                .alias("satellite_value")
            ),
            "observation flags",
        ),
        (lambda frame: pl.concat([frame, frame.head(1)]), "duplicate"),
    ],
)
def test_satellite_null_flags_nonfinite_values_and_duplicate_keys_are_not_repaired(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    readiness_generation, benchmark_generation, paths = _write_generations(
        tmp_path, satellite_context=mutation(_satellite_rows())
    )

    with pytest.raises(RuntimeError, match=message):
        satellite_value.load_micro_sensor_satellite_inputs(
            data_root=tmp_path,
            config=_config(readiness_generation, benchmark_generation, paths),
        )


def test_preparation_uses_one_complete_cohort_and_preserves_each_exclusion_reason(
    tmp_path: Path,
) -> None:
    prepared, _, _ = _prepared(tmp_path)

    assert prepared.source_rows == 550
    assert prepared.rows.height == 500
    assert prepared.rows["device_id"].n_unique() == 20
    assert prepared.rows["station_name"].n_unique() == 10
    assert prepared.rows["date"].n_unique() == 25
    assert prepared.coverage.to_dicts() == [
        {
            "source": "maiac_aod",
            "station_rows": 11,
            "observed_stations": 10,
            "unobserved_stations": 1,
            "linked_devices_sum": 22,
        },
        {
            "source": "s5p_no2",
            "station_rows": 11,
            "observed_stations": 11,
            "unobserved_stations": 0,
            "linked_devices_sum": 22,
        },
        {
            "source": "s5p_so2",
            "station_rows": 11,
            "observed_stations": 11,
            "unobserved_stations": 0,
            "linked_devices_sum": 22,
        },
    ]
    assert prepared.exclusions.to_dicts() == [
        {
            "station_name": "reference-10",
            "source_rows": 50,
            "devices": 2,
            "missing_sources": ["maiac_aod"],
            "reason": "one or more satellite sources unobserved",
        }
    ]
    assert prepared.held_dates.height == 25
    assert prepared.held_stations.height == 11
    assert prepared.held_stations["fold_index"].n_unique() == 10
    assert prepared.rows["station_fold"].null_count() == 0


def test_a_satellite_complete_station_missing_from_the_existing_folds_is_rejected(
    tmp_path: Path,
) -> None:
    membership = _fold_membership().filter(pl.col("group") != "reference-00")
    readiness_generation, benchmark_generation, paths = _write_generations(
        tmp_path, fold_membership=membership
    )
    config = _config(readiness_generation, benchmark_generation, paths)
    with pytest.raises(RuntimeError, match=r"absent from.*fold"):
        satellite_value.load_micro_sensor_satellite_inputs(data_root=tmp_path, config=config)


def test_grouped_evaluation_fits_four_serial_models_and_tests_each_cohort_row_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, config, _ = _prepared(tmp_path)
    calls: list[tuple[str, ...]] = []

    def observed_fit(
        train: pl.DataFrame,
        test: pl.DataFrame,
        features: tuple[str, ...],
        model: satellite_value.SatelliteContextModelConfig,
    ) -> np.ndarray:
        calls.append(features)
        return _observed_fit(train, test, features, model)

    monkeypatch.setattr(satellite_value, "_fit_predict", observed_fit)
    evaluation = satellite_value.evaluate_micro_sensor_satellite_value(prepared, config)

    assert evaluation.folds.height == 35
    assert evaluation.predictions.height == 1000
    assert len(calls) == 140
    assert set(calls) == set(satellite_value.LEARNED_FEATURE_SETS.values())
    assert evaluation.predictions.group_by("evaluation", "device_id", "hour").len()[
        "len"
    ].unique().to_list() == [1]
    assert evaluation.folds["train_membership_sha256"].str.len_chars().unique().to_list() == [64]
    assert evaluation.folds["test_membership_sha256"].str.len_chars().unique().to_list() == [64]
    for feature in (
        "pm25_mean",
        "humidity_mean",
        "temperature_mean",
        "maiac_aod",
        "s5p_no2",
        "s5p_so2",
    ):
        assert evaluation.predictions[feature].null_count() == 0


def test_scores_keep_both_weightings_and_all_paired_satellite_comparisons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, config, _ = _prepared(tmp_path)
    monkeypatch.setattr(satellite_value, "_fit_predict", _observed_fit)
    evaluation = satellite_value.evaluate_micro_sensor_satellite_value(prepared, config)

    scores = satellite_value.score_micro_sensor_satellite_predictions(evaluation.predictions)
    deltas = satellite_value.micro_sensor_satellite_value_deltas(scores, config)

    assert scores.height == 35 * 2 * 5
    assert deltas.height == 35 * 2 * 8
    assert set(scores["evaluation_unit"]) == {"device_hour", "reference_station_hour"}
    assert set(deltas["comparison"]) == {item[2] for item in satellite_value.COMPARISONS}
    satellite = deltas.filter(pl.col("comparison") == "micro_satellite_minus_micro_only")
    assert satellite["rmse_delta"].gt(0).all()
    assert not satellite["rmse_improved"].any()


def test_nonfinite_predictions_fail_instead_of_disappearing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, config, _ = _prepared(tmp_path)

    def nonfinite_fit(
        train: pl.DataFrame,
        test: pl.DataFrame,
        features: tuple[str, ...],
        model: satellite_value.SatelliteContextModelConfig,
    ) -> np.ndarray:
        values = _observed_fit(train, test, features, model)
        values[0] = np.nan
        return values

    monkeypatch.setattr(satellite_value, "_fit_predict", nonfinite_fit)
    with pytest.raises(RuntimeError, match="non-finite"):
        satellite_value.evaluate_micro_sensor_satellite_value(prepared, config)


def test_the_runner_rehashes_inputs_and_the_writer_publishes_exactly_eight_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness_generation, benchmark_generation, paths = _write_generations(tmp_path)
    config = _config(readiness_generation, benchmark_generation, paths)
    monkeypatch.setattr(satellite_value, "_fit_predict", _observed_fit)

    result = satellite_value.run_micro_sensor_satellite_value(
        data_root=tmp_path,
        config=config,
        generated_at="2026-08-12T00:00:00+00:00",
        git_sha="e" * 40,
        git_dirty=False,
    )
    destination = tmp_path / "published"
    written = satellite_value.write_micro_sensor_satellite_value_result(
        result, destination=destination
    )

    assert set(written) == {
        "coverage",
        "exclusions",
        "folds",
        "predictions",
        "scores",
        "deltas",
        "summary",
        "manifest",
    }
    assert {path.name for path in destination.iterdir()} == {
        "coverage.parquet",
        "exclusions.parquet",
        "folds.parquet",
        "predictions.parquet",
        "scores.parquet",
        "deltas.parquet",
        "summary.json",
        "manifest.json",
    }
    assert result.summary["source_rows"] == 550
    assert result.summary["cohort_rows"] == 500
    assert result.summary["excluded_rows"] == 50
    assert result.manifest["claim_boundary"] == satellite_value.CLAIM_BOUNDARY
    assert {item["path"] for item in result.manifest["inputs"]} == {
        path.relative_to(tmp_path).as_posix() for path in paths.values()
    }


def test_an_input_change_during_fits_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness_generation, benchmark_generation, paths = _write_generations(tmp_path)
    config = _config(readiness_generation, benchmark_generation, paths)
    calls = 0

    def mutating_fit(
        train: pl.DataFrame,
        test: pl.DataFrame,
        features: tuple[str, ...],
        model: satellite_value.SatelliteContextModelConfig,
    ) -> np.ndarray:
        nonlocal calls
        calls += 1
        if calls == 1:
            path = paths["benchmark_manifest"]
            path.write_bytes(path.read_bytes() + b" ")
        return _observed_fit(train, test, features, model)

    monkeypatch.setattr(satellite_value, "_fit_predict", mutating_fit)
    with pytest.raises(RuntimeError, match="input changed during"):
        satellite_value.run_micro_sensor_satellite_value(data_root=tmp_path, config=config)


@pytest.mark.parametrize("interrupt_call", [1, 2])
def test_an_interrupt_during_either_directory_rename_restores_the_previous_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_call: int,
) -> None:
    readiness_generation, benchmark_generation, paths = _write_generations(tmp_path)
    config = _config(readiness_generation, benchmark_generation, paths)
    monkeypatch.setattr(satellite_value, "_fit_predict", _observed_fit)
    result = satellite_value.run_micro_sensor_satellite_value(
        data_root=tmp_path,
        config=config,
        generated_at="2026-08-12T00:00:00+00:00",
        git_sha="e" * 40,
        git_dirty=False,
    )
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("previous", encoding="utf-8")
    real_replace = Path.replace
    calls = 0

    def interrupted_replace(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        moved = real_replace(path, target)
        if calls == interrupt_call:
            raise KeyboardInterrupt
        return moved

    monkeypatch.setattr(Path, "replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt):
        satellite_value.write_micro_sensor_satellite_value_result(result, destination=destination)

    assert destination.is_dir()
    assert (destination / "old.txt").read_text(encoding="utf-8") == "previous"
    assert not list(tmp_path.glob(".published.staging-*"))
    assert not list(tmp_path.glob(".published.backup-*"))


def test_an_interrupt_on_the_first_publish_leaves_no_partial_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness_generation, benchmark_generation, paths = _write_generations(tmp_path)
    config = _config(readiness_generation, benchmark_generation, paths)
    monkeypatch.setattr(satellite_value, "_fit_predict", _observed_fit)
    result = satellite_value.run_micro_sensor_satellite_value(
        data_root=tmp_path,
        config=config,
        generated_at="2026-08-12T00:00:00+00:00",
        git_sha="e" * 40,
        git_dirty=False,
    )
    destination = tmp_path / "first-publication"
    real_replace = Path.replace

    def interrupted_replace(path: Path, target: Path) -> Path:
        real_replace(path, target)
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt):
        satellite_value.write_micro_sensor_satellite_value_result(result, destination=destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".first-publication.staging-*"))
    assert not list(tmp_path.glob(".first-publication.backup-*"))
