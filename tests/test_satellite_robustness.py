"""M8 multi-year satellite robustness tests preserve the observed-row boundary."""

from __future__ import annotations

import importlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from twair.analysis.era5_value import ModelConfig
from twair.analysis.satellite import SatelliteAssociationResult
from twair.ingest.station_inventory import station_inventory_generation


def _robustness() -> Any:
    return importlib.import_module("twair.analysis.satellite_robustness")


def _model() -> ModelConfig:
    return ModelConfig(
        n_estimators=20,
        learning_rate=0.05,
        num_leaves=7,
        min_child_samples=2,
        n_jobs=1,
        seed=20260811,
    )


def _config(*, station_folds: int = 2) -> Any:
    return _robustness().SatelliteRobustnessConfig(
        years=(2024, 2025),
        quarter_folds=4,
        station_folds=station_folds,
        model=_model(),
    )


def _inventory(names: tuple[str, ...] = ("s1", "s2", "s3", "s4")) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": list(names),
            "airzone_official": ["north", "north", "central", None][: len(names)],
            "lon": [121.5, 121.2, 120.6, 120.2][: len(names)],
            "lat": [25.2, 25.0, 24.2, 22.6][: len(names)],
            "geo_source": ["MOENV"] * len(names),
            "geo_source_record_namespace": ["AQX_P_07"] * len(names),
            "geo_source_record_id": [f"site-{index}" for index in range(len(names))],
        },
        schema_overrides={"airzone_official": pl.String},
    )


def _panel(year: int, names: tuple[str, ...] = ("s1", "s2", "s3", "s4")) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for station_index, station in enumerate(names):
        for month in (1, 4, 7, 10):
            ground = 10.0 + station_index * 2.0 + month / 10 + (year - 2024)
            for source_index, source in enumerate(("maiac_aod", "s5p_no2", "s5p_so2")):
                rows.append(
                    {
                        "source": source,
                        "station_name": station,
                        "month": date(year, month, 1),
                        "satellite_value": source_index + station_index / 10 + month / 100,
                        "ground_value": ground,
                        "satellite_observed": True,
                        "ground_row_present": True,
                        "ground_meets_threshold": True,
                        "ground_observed": True,
                        "ground_withheld": False,
                        "pair_observed": True,
                    }
                )
    return pl.DataFrame(rows, schema_overrides={"month": pl.Date})


def _association(
    year: int,
    panel: pl.DataFrame,
    *,
    generation: str = "a" * 64,
) -> SatelliteAssociationResult:
    return SatelliteAssociationResult(
        panel=panel,
        coverage=pl.DataFrame(),
        association=pl.DataFrame(),
        station_context=pl.DataFrame(),
        month_context=pl.DataFrame(),
        manifest={
            "schema_version": 1,
            "analysis": "m8_satellite_association",
            "year": year,
            "mode": "generation",
            "inventory_generation_sha256": generation,
        },
    )


def _prepared() -> Any:
    module = _robustness()
    return module.prepare_satellite_robustness_rows(
        {
            2024: _association(2024, _panel(2024)),
            2025: _association(2025, _panel(2025)),
        },
        _inventory(),
        config=_config(),
    )


def _perfect_fit(
    _train: pl.DataFrame,
    test: pl.DataFrame,
    _features: tuple[str, ...],
    _model: ModelConfig,
) -> tuple[np.ndarray, np.ndarray]:
    truth = test["PM2.5"].to_numpy()
    return truth, truth.copy()


def _result() -> Any:
    return _robustness().SatelliteRobustnessResult(
        scores=pl.DataFrame({"feature_set": ["baseline"], "rmse": [1.0]}),
        deltas=pl.DataFrame({"comparison": ["baseline_aod_minus_baseline"]}),
        coverage=pl.DataFrame({"year": [2024], "station_month_rows": [1]}),
        station_folds=pl.DataFrame({"station_name": ["s1"], "station_fold": [0]}),
        summary={"evaluations": {}},
        manifest={"schema_version": 1, "complete": True},
    )


def test_the_shipped_config_fixes_two_years_serial_models_and_the_reviewed_features() -> None:
    module = _robustness()
    config = module.load_satellite_robustness_config()

    assert config.years == (2024, 2025)
    assert config.quarter_folds == 4
    assert config.station_folds == 10
    assert config.model == ModelConfig(
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=10,
        n_jobs=1,
        seed=20260811,
    )
    assert set(module.SATELLITE_FEATURE_SETS) == {
        "baseline",
        "baseline_aod",
        "baseline_no2",
        "baseline_so2",
        "all_satellite",
    }


def test_the_config_rejects_parallel_lightgbm_and_non_reviewed_years() -> None:
    module = _robustness()
    raw: dict[str, Any] = {
        "analysis": {
            "years": [2023, 2025],
            "quarter_folds": 4,
            "station_folds": 2,
            "model": {
                "n_estimators": 20,
                "learning_rate": 0.05,
                "num_leaves": 7,
                "min_child_samples": 2,
                "n_jobs": 2,
                "seed": 1,
            },
        }
    }

    with pytest.raises(Exception, match=r"years|n_jobs"):
        module.load_satellite_robustness_config(raw)


def test_the_config_rejects_any_station_fold_or_model_protocol_drift() -> None:
    module = _robustness()
    raw: dict[str, Any] = {
        "analysis": {
            "years": [2024, 2025],
            "quarter_folds": 4,
            "station_folds": 9,
            "model": {
                "n_estimators": 200,
                "learning_rate": 0.05,
                "num_leaves": 31,
                "min_child_samples": 10,
                "n_jobs": 1,
                "seed": 20260811,
            },
        }
    }

    with pytest.raises(Exception, match="station_folds"):
        module.load_satellite_robustness_config(raw)

    raw["analysis"]["station_folds"] = 10
    raw["analysis"]["model"]["num_leaves"] = 30
    with pytest.raises(Exception, match="model"):
        module.load_satellite_robustness_config(raw)


def test_the_common_frames_measure_each_years_exclusions_before_filtering() -> None:
    panel_2024 = _panel(2024)
    panel_2025 = _panel(2025)
    panel_2024 = (
        panel_2024.with_columns(
            pl.when(
                (pl.col("station_name") == "s1")
                & (pl.col("month") == date(2024, 1, 1))
                & (pl.col("source") == "maiac_aod")
            )
            .then(None)
            .otherwise(pl.col("satellite_value"))
            .alias("satellite_value")
        )
        .with_columns(pl.col("satellite_value").is_not_null().alias("satellite_observed"))
        .with_columns(
            (pl.col("satellite_observed") & pl.col("ground_observed")).alias("pair_observed")
        )
    )
    panel_2025 = (
        panel_2025.with_columns(
            pl.when((pl.col("station_name") == "s2") & (pl.col("month") == date(2025, 4, 1)))
            .then(False)
            .otherwise(pl.col("ground_row_present"))
            .alias("ground_row_present"),
            pl.when((pl.col("station_name") == "s2") & (pl.col("month") == date(2025, 4, 1)))
            .then(None)
            .when((pl.col("station_name") == "s3") & (pl.col("month") == date(2025, 7, 1)))
            .then(None)
            .otherwise(pl.col("ground_value"))
            .alias("ground_value"),
            pl.when((pl.col("station_name") == "s2") & (pl.col("month") == date(2025, 4, 1)))
            .then(None)
            .when((pl.col("station_name") == "s3") & (pl.col("month") == date(2025, 7, 1)))
            .then(False)
            .otherwise(pl.col("ground_meets_threshold"))
            .alias("ground_meets_threshold"),
        )
        .with_columns(
            (pl.col("ground_row_present") & pl.col("ground_value").is_not_null()).alias(
                "ground_observed"
            ),
            (pl.col("ground_row_present") & pl.col("ground_value").is_null()).alias(
                "ground_withheld"
            ),
        )
        .with_columns(
            (pl.col("satellite_observed") & pl.col("ground_observed")).alias("pair_observed")
        )
    )

    prepared = _robustness().prepare_satellite_robustness_rows(
        {
            2024: _association(2024, panel_2024),
            2025: _association(2025, panel_2025),
        },
        _inventory(),
        config=_config(),
    )
    coverage = {row["year"]: row for row in prepared.coverage.iter_rows(named=True)}

    assert coverage[2024]["station_month_rows"] == 16
    assert coverage[2024]["maiac_null_rows"] == 1
    assert coverage[2024]["common_complete_rows"] == 15
    assert coverage[2025]["ground_absent_rows"] == 1
    assert coverage[2025]["ground_withheld_rows"] == 1
    assert coverage[2025]["common_complete_rows"] == 14
    assert prepared.values.height == 29


def test_missing_a_reviewed_year_is_rejected_before_any_model_can_run() -> None:
    with pytest.raises(RuntimeError, match="year"):
        _robustness().prepare_satellite_robustness_rows(
            {2025: _association(2025, _panel(2025))},
            _inventory(),
            config=_config(),
        )


def test_cross_year_station_cohort_exclusions_remain_explicit_in_each_years_coverage() -> None:
    panel_2025 = (
        _panel(2025)
        .with_columns(
            pl.when(pl.col("station_name") == "s4")
            .then(None)
            .otherwise(pl.col("satellite_value"))
            .alias("satellite_value")
        )
        .with_columns(pl.col("satellite_value").is_not_null().alias("satellite_observed"))
        .with_columns(
            (pl.col("satellite_observed") & pl.col("ground_observed")).alias("pair_observed")
        )
    )

    prepared = _robustness().prepare_satellite_robustness_rows(
        {
            2024: _association(2024, _panel(2024)),
            2025: _association(2025, panel_2025),
        },
        _inventory(),
        config=_config(),
    )
    coverage = {row["year"]: row for row in prepared.coverage.iter_rows(named=True)}

    assert coverage[2024]["common_complete_rows"] == 16
    assert coverage[2024]["cross_year_common_rows"] == 12
    assert coverage[2024]["cross_year_excluded_rows"] == 4
    assert coverage[2025]["common_complete_rows"] == 12
    assert coverage[2025]["cross_year_common_rows"] == 12
    assert coverage[2025]["cross_year_excluded_rows"] == 0


def test_year_replication_keeps_each_years_complete_stations_outside_the_transfer_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _robustness()
    panel_2025 = (
        _panel(2025)
        .with_columns(
            pl.when(pl.col("station_name") == "s4")
            .then(None)
            .otherwise(pl.col("satellite_value"))
            .alias("satellite_value")
        )
        .with_columns(pl.col("satellite_value").is_not_null().alias("satellite_observed"))
        .with_columns(
            (pl.col("satellite_observed") & pl.col("ground_observed")).alias("pair_observed")
        )
    )
    prepared = module.prepare_satellite_robustness_rows(
        {
            2024: _association(2024, _panel(2024)),
            2025: _association(2025, panel_2025),
        },
        _inventory(),
        config=_config(),
    )
    monkeypatch.setattr(module, "_fit_predict", _perfect_fit)

    scores = module.evaluate_satellite_robustness(
        prepared.values,
        prepared.station_folds,
        config=_config(),
        yearly_values=prepared.yearly_values,
    )
    yearly_baseline = scores.filter(
        (pl.col("evaluation") == "year_replication") & (pl.col("feature_set") == "baseline")
    )

    assert {
        row["test_year"]: row["n_test"]
        for row in yearly_baseline.group_by("test_year")
        .agg(pl.col("n_test").sum())
        .iter_rows(named=True)
    } == {2024: 16, 2025: 12}
    assert prepared.station_folds["station_name"].to_list() == ["s1", "s2", "s3"]


def test_station_membership_is_deterministic_and_preserves_the_unclassified_stratum() -> None:
    first = _prepared().station_folds
    second = (
        _robustness()
        .prepare_satellite_robustness_rows(
            {
                2024: _association(2024, _panel(2024)),
                2025: _association(2025, _panel(2025)),
            },
            _inventory().reverse(),
            config=_config(),
        )
        .station_folds
    )

    assert first.equals(second)
    assert first["station_name"].n_unique() == first.height
    assert sorted(first["station_fold"].unique().to_list()) == [0, 1]
    assert (
        first.filter(pl.col("station_name") == "s4").row(0, named=True)["airzone_official"] is None
    )


def test_a_station_set_that_cannot_support_two_folds_is_rejected_not_repaired() -> None:
    with pytest.raises(RuntimeError, match="at least two"):
        _robustness().prepare_satellite_robustness_rows(
            {
                2024: _association(2024, _panel(2024, ("s1",))),
                2025: _association(2025, _panel(2025, ("s1",))),
            },
            _inventory(("s1",)),
            config=_config(),
        )


def test_every_candidate_and_baseline_score_uses_the_same_rows_without_station_leakage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _robustness()
    prepared = _prepared()
    membership = dict(prepared.station_folds.select("station_name", "station_fold").iter_rows())
    calls: list[tuple[set[tuple[str, date]], set[tuple[str, date]], tuple[str, ...]]] = []

    def observed_fit(
        train: pl.DataFrame,
        test: pl.DataFrame,
        features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        train_keys = set(train.select("station_name", "month").iter_rows())
        test_keys = set(test.select("station_name", "month").iter_rows())
        assert not train_keys & test_keys
        train_years = {month.year for _, month in train_keys}
        test_years = {month.year for _, month in test_keys}
        test_folds = {membership[station] for station, _ in test_keys}
        train_folds = {membership[station] for station, _ in train_keys}
        if len(test_years) == 1 and train_years != test_years and len(test_folds) == 1:
            assert train_folds.isdisjoint(test_folds)
        calls.append((train_keys, test_keys, features))
        truth = test["PM2.5"].to_numpy()
        return truth, truth.copy()

    monkeypatch.setattr(module, "_fit_predict", observed_fit)
    scores = module.evaluate_satellite_robustness(
        prepared.values,
        prepared.station_folds,
        config=_config(),
    )

    assert scores.height == 14 * len(module.SATELLITE_FEATURE_SETS)
    assert set(scores["evaluation"].unique().to_list()) == {
        "year_replication",
        "cross_year_replication",
        "station_year_transfer",
    }
    assert set(
        scores.filter(pl.col("evaluation") == "cross_year_replication")["fold"].unique().to_list()
    ) == {"2024_to_2025", "2025_to_2024"}
    for _, group in scores.group_by(
        "evaluation", "train_year", "test_year", "station_fold", "fold", maintain_order=True
    ):
        assert group["n_train"].n_unique() == 1
        assert group["n_test"].n_unique() == 1
    for offset in range(0, len(calls), len(module.SATELLITE_FEATURE_SETS)):
        block = calls[offset : offset + len(module.SATELLITE_FEATURE_SETS)]
        assert len({tuple(sorted(item[0])) for item in block}) == 1
        assert len({tuple(sorted(item[1])) for item in block}) == 1


def test_duplicate_or_missing_fold_membership_is_rejected() -> None:
    module = _robustness()
    prepared = _prepared()
    duplicate = pl.concat([prepared.station_folds, prepared.station_folds.head(1)])

    with pytest.raises(RuntimeError, match="membership"):
        module.evaluate_satellite_robustness(prepared.values, duplicate, config=_config())
    with pytest.raises(RuntimeError, match="membership"):
        module.evaluate_satellite_robustness(
            prepared.values,
            prepared.station_folds.head(prepared.station_folds.height - 1),
            config=_config(),
        )


def test_nonfinite_inputs_are_rejected_before_lightgbm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _robustness()
    prepared = _prepared()
    nonfinite = prepared.values.with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(float("nan"))
        .otherwise(pl.col("maiac_aod"))
        .alias("maiac_aod")
    )
    monkeypatch.setattr(module, "_fit_predict", _perfect_fit)

    with pytest.raises(RuntimeError, match="non-finite input"):
        module.evaluate_satellite_robustness(nonfinite, prepared.station_folds, config=_config())


def test_nonfinite_predictions_and_metrics_are_rejected_before_serialisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _robustness()
    prepared = _prepared()

    def nonfinite_prediction(
        _train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        truth = test["PM2.5"].to_numpy()
        prediction = truth.copy()
        prediction[0] = np.nan
        return truth, prediction

    monkeypatch.setattr(module, "_fit_predict", nonfinite_prediction)
    with pytest.raises(RuntimeError, match="non-finite predictions"):
        module.evaluate_satellite_robustness(
            prepared.values, prepared.station_folds, config=_config()
        )

    monkeypatch.setattr(module, "_fit_predict", _perfect_fit)
    constant = prepared.values.with_columns(pl.lit(1.0).alias("PM2.5"))
    with pytest.raises(RuntimeError, match="non-finite metrics"):
        module.evaluate_satellite_robustness(constant, prepared.station_folds, config=_config())


def test_paired_deltas_keep_the_direction_and_station_fold_boundaries() -> None:
    module = _robustness()
    rows: list[dict[str, object]] = []
    for feature_index, feature_set in enumerate(module.SATELLITE_FEATURE_SETS):
        rows.append(
            {
                "evaluation": "station_year_transfer",
                "train_year": 2024,
                "test_year": 2025,
                "station_fold": 1,
                "fold": "2024_to_2025_station_01",
                "feature_set": feature_set,
                "n_train": 20,
                "n_test": 8,
                "rmse": 2.0 - feature_index / 10,
                "mae": 1.5 - feature_index / 10,
                "r2": 0.2 + feature_index / 10,
                "fit_seconds": 0.01,
            }
        )
    scores = pl.DataFrame(rows, schema_overrides={"station_fold": pl.Int64})

    deltas = module.satellite_robustness_metric_deltas(scores)
    summary = module.summarise_satellite_robustness_deltas(deltas)
    all_satellite = deltas.filter(pl.col("candidate") == "all_satellite").row(0, named=True)

    assert all_satellite["station_fold"] == 1
    assert all_satellite["rmse_delta"] == pytest.approx(-0.4)
    assert (
        summary["evaluations"]["station_year_transfer"]["year_pairs"]["2024_to_2025"][
            "all_satellite_minus_baseline"
        ]["both_improved"]
        == 1
    )


def test_the_writer_replaces_stale_output_and_recovers_after_either_interrupted_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _robustness()
    destination = tmp_path / "m8_satellite_robustness"
    destination.mkdir()
    baseline = destination / "baseline.txt"
    baseline.write_text("complete", encoding="utf-8")
    real_replace = Path.replace
    calls = 0

    def interrupt_after_move(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        moved = real_replace(path, target)
        if calls == 1:
            raise KeyboardInterrupt
        return moved

    monkeypatch.setattr(Path, "replace", interrupt_after_move)
    with pytest.raises(KeyboardInterrupt):
        module.write_satellite_robustness_result(_result(), destination=destination)

    assert baseline.read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".m8_satellite_robustness.staging-*"))
    assert not list(tmp_path.glob(".m8_satellite_robustness.backup-*"))


def test_an_interrupt_before_replacing_the_previous_output_restores_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _robustness()
    destination = tmp_path / "m8_satellite_robustness"
    destination.mkdir()
    baseline = destination / "baseline.txt"
    baseline.write_text("complete", encoding="utf-8")
    real_replace = Path.replace
    calls = 0

    def interrupt_second_rename(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupt_second_rename)
    with pytest.raises(KeyboardInterrupt):
        module.write_satellite_robustness_result(_result(), destination=destination)

    assert baseline.read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".m8_satellite_robustness.staging-*"))
    assert not list(tmp_path.glob(".m8_satellite_robustness.backup-*"))


def test_the_writer_recovers_stale_swap_siblings_and_replaces_the_previous_output(
    tmp_path: Path,
) -> None:
    module = _robustness()
    destination = tmp_path / "m8_satellite_robustness"
    destination.mkdir()
    (destination / "baseline.txt").write_text("complete", encoding="utf-8")
    backup = destination.with_name(".m8_satellite_robustness.backup-interrupted")
    destination.replace(backup)
    staged = destination.with_name(".m8_satellite_robustness.staging-interrupted")
    staged.mkdir()
    (staged / "partial.txt").write_text("partial", encoding="utf-8")

    written = module.write_satellite_robustness_result(_result(), destination=destination)

    assert json.loads(written["manifest"].read_text(encoding="utf-8"))["complete"] is True
    assert not backup.exists()
    assert not staged.exists()
    assert {path.name for path in destination.iterdir()} == {
        "scores.parquet",
        "paired_deltas.parquet",
        "coverage.parquet",
        "station_folds.parquet",
        "summary.json",
        "manifest.json",
    }


def _write_association_output(
    root: Path,
    *,
    year: int,
    generation: str,
    panel: pl.DataFrame,
) -> Path:
    destination = root / "outputs" / "m8_satellite" / "generations" / generation / f"year={year}"
    destination.mkdir(parents=True)
    panel.write_parquet(destination / "panel.parquet")
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "analysis": "m8_satellite_association",
                "year": year,
                "mode": "generation",
                "inventory_generation_sha256": generation,
            }
        ),
        encoding="utf-8",
    )
    return destination


def test_the_runner_binds_each_existing_association_year_and_stable_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _robustness()
    inventory = _inventory()
    generation = station_inventory_generation(inventory).sha256
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    station_path = tmp_path / "outputs" / "qc" / "stations.parquet"
    station_path.parent.mkdir(parents=True)
    inventory.write_parquet(station_path)
    for year in (2024, 2025):
        _write_association_output(tmp_path, year=year, generation=generation, panel=_panel(year))
    monkeypatch.setattr(module, "_fit_predict", _perfect_fit)

    result = module.run_satellite_robustness(
        data_root=tmp_path,
        generation_sha256=generation,
        config=_config(),
        generated_at="2026-08-11T22:00:00+00:00",
    )

    assert result.manifest["complete"] is True
    assert result.manifest["years"] == [2024, 2025]
    assert result.manifest["inventory_generation_sha256"] == generation
    assert result.manifest["common_stations"] == ["s1", "s2", "s3", "s4"]
    assert result.manifest["association_inputs"] == {
        "2024": "outputs/m8_satellite/generations/" + generation + "/year=2024",
        "2025": "outputs/m8_satellite/generations/" + generation + "/year=2025",
    }
    assert len(result.manifest["input_files"]) == 5
    assert all(len(item["sha256"]) == 64 for item in result.manifest["input_files"])


def test_the_runner_rejects_a_missing_year_wrong_generation_and_an_input_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _robustness()
    inventory = _inventory()
    generation = station_inventory_generation(inventory).sha256
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    station_path = tmp_path / "outputs" / "qc" / "stations.parquet"
    station_path.parent.mkdir(parents=True)
    inventory.write_parquet(station_path)
    _write_association_output(tmp_path, year=2024, generation=generation, panel=_panel(2024))

    with pytest.raises(FileNotFoundError, match="2025"):
        module.run_satellite_robustness(
            data_root=tmp_path, generation_sha256=generation, config=_config()
        )

    directory = _write_association_output(
        tmp_path, year=2025, generation=generation, panel=_panel(2025)
    )
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["year"] = 2024
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="year"):
        module.run_satellite_robustness(
            data_root=tmp_path, generation_sha256=generation, config=_config()
        )

    manifest["year"] = 2025
    manifest["inventory_generation_sha256"] = "b" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="generation"):
        module.run_satellite_robustness(
            data_root=tmp_path, generation_sha256=generation, config=_config()
        )

    manifest["inventory_generation_sha256"] = generation
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def mutating_fit(
        _train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        (directory / "panel.parquet").write_bytes(b"changed")
        truth = test["PM2.5"].to_numpy()
        return truth, truth.copy()

    monkeypatch.setattr(module, "_fit_predict", mutating_fit)
    with pytest.raises(RuntimeError, match="input changed"):
        module.run_satellite_robustness(
            data_root=tmp_path, generation_sha256=generation, config=_config()
        )


def test_the_runner_makes_no_network_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    module = _robustness()
    inventory = _inventory()
    generation = station_inventory_generation(inventory).sha256
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    station_path = tmp_path / "outputs" / "qc" / "stations.parquet"
    station_path.parent.mkdir(parents=True)
    inventory.write_parquet(station_path)
    for year in (2024, 2025):
        _write_association_output(tmp_path, year=year, generation=generation, panel=_panel(year))

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("satellite robustness made a network request")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(module, "_fit_predict", _perfect_fit)

    result = module.run_satellite_robustness(
        data_root=tmp_path, generation_sha256=generation, config=_config()
    )

    assert result.scores.height == 70
