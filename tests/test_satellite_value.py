"""M8 satellite value tests predictive generalisation without creating a fused product."""

from __future__ import annotations

import json
from datetime import date
from hashlib import sha256
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from twair.analysis import satellite_value
from twair.analysis.era5_value import ModelConfig
from twair.analysis.satellite import SatelliteAssociationResult
from twair.analysis.satellite_value import (
    SATELLITE_FEATURE_SETS,
    SatelliteValueConfig,
    SatelliteValueResult,
    evaluate_satellite_value,
    load_satellite_value_config,
    prepare_satellite_value_rows,
    run_satellite_value,
    satellite_metric_deltas,
    summarise_satellite_deltas,
    write_satellite_value_result,
)
from twair.ingest.station_inventory import station_inventory_generation


def _model() -> ModelConfig:
    return ModelConfig(
        n_estimators=20,
        learning_rate=0.05,
        num_leaves=7,
        min_child_samples=2,
        n_jobs=1,
        seed=20260811,
    )


def _config(*, station_folds: int = 2) -> SatelliteValueConfig:
    return SatelliteValueConfig(
        year=2025,
        quarter_folds=4,
        station_folds=station_folds,
        model=_model(),
    )


def _inventory(names: tuple[str, ...] = ("富貴角", "馬公", "忠明", "前金")) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": list(names),
            "airzone_official": ["北部", "離島", "中部", None][: len(names)],
            "lon": [121.536, 119.566, 120.641, 120.288][: len(names)],
            "lat": [25.297, 23.569, 24.151, 22.632][: len(names)],
            "geo_source": ["MOENV"] * len(names),
            "geo_source_record_namespace": ["AQX_P_07"] * len(names),
            "geo_source_record_id": [f"site-{index}" for index in range(len(names))],
        },
        schema_overrides={"airzone_official": pl.String},
    )


def _panel(
    names: tuple[str, ...] = ("富貴角", "馬公", "忠明", "前金"),
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    sources = ("maiac_aod", "s5p_no2", "s5p_so2")
    for station_index, station in enumerate(names):
        for month in (1, 4, 7, 10):
            ground = 10.0 + station_index * 2.0 + month / 10
            for source_index, source in enumerate(sources):
                rows.append(
                    {
                        "source": source,
                        "station_name": station,
                        "month": date(2025, month, 1),
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


def _association(panel: pl.DataFrame, *, generation: str = "a" * 64) -> SatelliteAssociationResult:
    return SatelliteAssociationResult(
        panel=panel,
        coverage=pl.DataFrame(),
        association=pl.DataFrame(),
        station_context=pl.DataFrame(),
        month_context=pl.DataFrame(),
        manifest={
            "schema_version": 1,
            "year": 2025,
            "mode": "generation",
            "inventory_generation_sha256": generation,
            "upstream": {},
        },
    )


def test_the_shipped_config_fixes_serial_models_and_all_held_out_designs() -> None:
    config = load_satellite_value_config()

    assert config.year == 2025
    assert config.quarter_folds == 4
    assert config.station_folds == 10
    assert config.model.n_jobs == 1
    assert set(SATELLITE_FEATURE_SETS) == {
        "baseline",
        "baseline_aod",
        "baseline_no2",
        "baseline_so2",
        "all_satellite",
    }


def test_the_common_frame_measures_every_exclusion_before_filtering() -> None:
    panel = _panel()
    keys = panel.select("station_name", "month").unique().sort("station_name", "month")
    source_null = keys.row(0, named=True)
    ground_absent = keys.row(1, named=True)
    ground_withheld = keys.row(2, named=True)
    panel = (
        panel.with_columns(
            pl.when(
                (pl.col("station_name") == source_null["station_name"])
                & (pl.col("month") == source_null["month"])
                & (pl.col("source") == "maiac_aod")
            )
            .then(None)
            .otherwise(pl.col("satellite_value"))
            .alias("satellite_value"),
            pl.when(
                (pl.col("station_name") == ground_absent["station_name"])
                & (pl.col("month") == ground_absent["month"])
            )
            .then(False)
            .otherwise(pl.col("ground_row_present"))
            .alias("ground_row_present"),
            pl.when(
                (pl.col("station_name") == ground_absent["station_name"])
                & (pl.col("month") == ground_absent["month"])
            )
            .then(None)
            .when(
                (pl.col("station_name") == ground_withheld["station_name"])
                & (pl.col("month") == ground_withheld["month"])
            )
            .then(None)
            .otherwise(pl.col("ground_value"))
            .alias("ground_value"),
            pl.when(
                (pl.col("station_name") == ground_absent["station_name"])
                & (pl.col("month") == ground_absent["month"])
            )
            .then(None)
            .when(
                (pl.col("station_name") == ground_withheld["station_name"])
                & (pl.col("month") == ground_withheld["month"])
            )
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
            (pl.col("satellite_value").is_not_null()).alias("satellite_observed"),
        )
        .with_columns(
            (pl.col("satellite_observed") & pl.col("ground_observed")).alias("pair_observed")
        )
    )

    prepared = prepare_satellite_value_rows(panel, _inventory(), config=_config())
    coverage = prepared.coverage.row(0, named=True)

    assert coverage["station_month_rows"] == 16
    assert coverage["maiac_null_rows"] == 1
    assert coverage["s5p_no2_null_rows"] == 0
    assert coverage["s5p_so2_null_rows"] == 0
    assert coverage["ground_absent_rows"] == 1
    assert coverage["ground_withheld_rows"] == 1
    assert coverage["coordinate_missing_rows"] == 0
    assert coverage["common_complete_rows"] == 13
    assert prepared.values.height == 13
    assert prepared.values.null_count().sum_horizontal().item() == 0


def test_ground_state_must_be_identical_across_the_three_source_rows() -> None:
    panel = _panel().with_columns(
        pl.when((pl.col("source") == "s5p_so2") & (pl.col("station_name") == "富貴角"))
        .then(pl.col("ground_value") + 1)
        .otherwise(pl.col("ground_value"))
        .alias("ground_value")
    )

    with pytest.raises(RuntimeError, match="ground state"):
        prepare_satellite_value_rows(panel, _inventory(), config=_config())


def test_missing_source_rows_are_not_silently_treated_as_source_nulls() -> None:
    panel = _panel().filter(
        ~((pl.col("source") == "s5p_so2") & (pl.col("station_name") == "富貴角"))
    )

    with pytest.raises(RuntimeError, match="complete source key"):
        prepare_satellite_value_rows(panel, _inventory(), config=_config())


def test_a_null_airzone_remains_an_unclassified_fold_stratum() -> None:
    prepared = prepare_satellite_value_rows(_panel(), _inventory(), config=_config())
    row = prepared.station_folds.filter(pl.col("station_name") == "前金").row(0, named=True)

    assert row["airzone_official"] is None
    assert row["geo_source"] == "MOENV"
    assert sorted(prepared.station_folds["station_fold"].unique().to_list()) == [0, 1]


def test_a_complete_station_cannot_lose_its_reviewed_geography_provenance() -> None:
    inventory = _inventory().with_columns(
        pl.when(pl.col("station_name") == "富貴角")
        .then(None)
        .otherwise(pl.col("geo_source_record_id"))
        .alias("geo_source_record_id")
    )

    with pytest.raises(RuntimeError, match="geography provenance"):
        prepare_satellite_value_rows(_panel(), inventory, config=_config())


def test_nonfinite_coordinates_are_measured_and_excluded_not_repaired() -> None:
    inventory = _inventory().with_columns(
        pl.when(pl.col("station_name") == "前金")
        .then(float("nan"))
        .otherwise(pl.col("lon"))
        .alias("lon")
    )

    prepared = prepare_satellite_value_rows(_panel(), inventory, config=_config())
    coverage = prepared.coverage.row(0, named=True)

    assert coverage["coordinate_missing_rows"] == 4
    assert coverage["common_complete_rows"] == 12
    assert "前金" not in prepared.values["station_name"].unique().to_list()


def test_every_held_out_design_uses_identical_rows_and_has_no_key_leakage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_satellite_value_rows(_panel(), _inventory(), config=_config())
    fold_by_station = dict(
        prepared.station_folds.select("station_name", "station_fold").iter_rows()
    )
    calls: list[tuple[set[tuple[str, date]], set[tuple[str, date]], tuple[str, ...]]] = []

    def perfect_fit(
        train: pl.DataFrame,
        test: pl.DataFrame,
        features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        train_keys = set(train.select("station_name", "month").iter_rows())
        test_keys = set(test.select("station_name", "month").iter_rows())
        assert not train_keys & test_keys
        train_quarters = {(month.month - 1) // 3 for _, month in train_keys}
        test_quarters = {(month.month - 1) // 3 for _, month in test_keys}
        train_station_folds = {fold_by_station[station] for station, _ in train_keys}
        test_station_folds = {fold_by_station[station] for station, _ in test_keys}
        if len(test_quarters) == 1:
            assert train_quarters.isdisjoint(test_quarters)
        if len(test_station_folds) == 1:
            assert train_station_folds.isdisjoint(test_station_folds)
        calls.append((train_keys, test_keys, features))
        truth = test["PM2.5"].to_numpy()
        return truth, truth.copy()

    monkeypatch.setattr(satellite_value, "_fit_predict", perfect_fit)
    scores = evaluate_satellite_value(
        prepared.values,
        prepared.station_folds,
        config=_config(),
    )

    assert scores.height == (4 + 2 + 8) * len(SATELLITE_FEATURE_SETS)
    assert set(scores["evaluation"].unique().to_list()) == {
        "quarter_transfer",
        "station_transfer",
        "spatiotemporal_transfer",
    }
    assert scores.filter(pl.col("feature_set") == "baseline")["n_test"].sum() == 48
    assert len(calls) == scores.height
    assert (
        scores.select("rmse", "mae", "r2").to_numpy().tolist() == [[0.0, 0.0, 1.0]] * scores.height
    )


def test_a_prediction_for_different_test_truth_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_satellite_value_rows(_panel(), _inventory(), config=_config())

    def wrong_truth(
        _train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        truth = test["PM2.5"].to_numpy()[::-1]
        return truth, truth.copy()

    monkeypatch.setattr(satellite_value, "_fit_predict", wrong_truth)

    with pytest.raises(RuntimeError, match="different test rows"):
        evaluate_satellite_value(
            prepared.values,
            prepared.station_folds,
            config=_config(),
        )


def test_a_nonfinite_prediction_is_rejected_before_metrics_are_computed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_satellite_value_rows(_panel(), _inventory(), config=_config())

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

    monkeypatch.setattr(satellite_value, "_fit_predict", nonfinite_prediction)

    with pytest.raises(RuntimeError, match="non-finite predictions"):
        evaluate_satellite_value(
            prepared.values,
            prepared.station_folds,
            config=_config(),
        )


def test_nonfinite_fold_metrics_are_rejected_before_json_serialisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_satellite_value_rows(_panel(), _inventory(), config=_config())
    constant = prepared.values.with_columns(pl.lit(1.0).alias("PM2.5"))

    def constant_fit(
        _train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        truth = test["PM2.5"].to_numpy()
        return truth, truth.copy()

    monkeypatch.setattr(satellite_value, "_fit_predict", constant_fit)

    with pytest.raises(RuntimeError, match="non-finite metrics"):
        evaluate_satellite_value(
            constant,
            prepared.station_folds,
            config=_config(),
        )


def test_a_one_row_spatiotemporal_fold_is_rejected_not_scored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ("富貴角", "馬公")
    prepared = prepare_satellite_value_rows(
        _panel(names),
        _inventory(names),
        config=_config(station_folds=2),
    )

    def perfect_fit(
        _train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        truth = test["PM2.5"].to_numpy()
        return truth, truth.copy()

    monkeypatch.setattr(satellite_value, "_fit_predict", perfect_fit)

    with pytest.raises(RuntimeError, match=r"spatiotemporal_transfer.*too few"):
        evaluate_satellite_value(
            prepared.values,
            prepared.station_folds,
            config=_config(station_folds=2),
        )


def test_candidate_deltas_are_paired_to_the_same_baseline_fold() -> None:
    rows: list[dict[str, object]] = []
    for evaluation, fold in (
        ("quarter_transfer", "quarter_0"),
        ("station_transfer", "station_00"),
    ):
        for feature_index, feature_set in enumerate(SATELLITE_FEATURE_SETS):
            rows.append(
                {
                    "evaluation": evaluation,
                    "fold": fold,
                    "quarter_fold": 0 if evaluation == "quarter_transfer" else None,
                    "station_fold": 0 if evaluation == "station_transfer" else None,
                    "feature_set": feature_set,
                    "n_train": 12,
                    "n_test": 4,
                    "rmse": 2.0 - feature_index / 10,
                    "mae": 1.5 - feature_index / 10,
                    "r2": 0.2 + feature_index / 10,
                    "fit_seconds": 0.01,
                }
            )
    scores = pl.DataFrame(rows, schema_overrides={"station_fold": pl.Int64})

    deltas = satellite_metric_deltas(scores)
    summary = summarise_satellite_deltas(deltas)

    assert deltas.height == 8
    assert set(deltas["reference"].to_list()) == {"baseline"}
    assert deltas.filter(pl.col("candidate") == "all_satellite")[
        "rmse_delta"
    ].to_list() == pytest.approx([-0.4, -0.4])
    assert (
        summary["evaluations"]["quarter_transfer"]["all_satellite_minus_baseline"]["both_improved"]
        == 1
    )
    assert summary["overall"]["all_satellite_minus_baseline"] == {
        "folds": 2,
        "test_rows": 8,
        "median_rmse_delta": pytest.approx(-0.4),
        "median_mae_delta": pytest.approx(-0.4),
        "median_r2_delta": pytest.approx(0.4),
        "both_improved": 2,
        "both_worse": 0,
        "exact_tie": 0,
        "mixed": 0,
    }


def test_the_writer_atomically_replaces_stale_output(tmp_path: Path) -> None:
    destination = tmp_path / "m8_satellite_value"
    destination.mkdir()
    (destination / "stale.txt").write_text("partial", encoding="utf-8")
    result = SatelliteValueResult(
        scores=pl.DataFrame({"feature_set": ["baseline"], "rmse": [1.0]}),
        deltas=pl.DataFrame({"comparison": ["baseline_aod_minus_baseline"]}),
        coverage=pl.DataFrame({"station_month_rows": [1]}),
        station_folds=pl.DataFrame({"station_name": ["富貴角"], "station_fold": [0]}),
        summary={"evaluations": {}},
        manifest={"schema_version": 1, "complete": True},
    )

    written = write_satellite_value_result(result, destination=destination)

    assert set(written) == {
        "scores",
        "paired_deltas",
        "coverage",
        "station_folds",
        "summary",
        "manifest",
    }
    assert {path.name for path in destination.iterdir()} == {
        "scores.parquet",
        "paired_deltas.parquet",
        "coverage.parquet",
        "station_folds.parquet",
        "summary.json",
        "manifest.json",
    }
    assert json.loads(written["manifest"].read_text(encoding="utf-8"))["complete"] is True


def test_a_keyboard_interrupt_during_swap_restores_the_previous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "m8_satellite_value"
    destination.mkdir()
    baseline = destination / "baseline.txt"
    baseline.write_text("complete", encoding="utf-8")
    result = SatelliteValueResult(
        scores=pl.DataFrame({"feature_set": ["baseline"], "rmse": [1.0]}),
        deltas=pl.DataFrame({"comparison": ["baseline_aod_minus_baseline"]}),
        coverage=pl.DataFrame({"station_month_rows": [1]}),
        station_folds=pl.DataFrame({"station_name": ["富貴角"], "station_fold": [0]}),
        summary={"overall": {}, "evaluations": {}},
        manifest={"schema_version": 1, "complete": True},
    )
    real_replace = Path.replace
    replace_calls = 0

    def interrupt_second_replace(path: Path, target: Path) -> Path:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise KeyboardInterrupt
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupt_second_replace)

    with pytest.raises(KeyboardInterrupt):
        write_satellite_value_result(result, destination=destination)

    assert baseline.read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".m8_satellite_value.staging-*"))
    assert not list(tmp_path.glob(".m8_satellite_value.backup-*"))


def test_an_interrupt_after_moving_the_previous_output_still_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "m8_satellite_value"
    destination.mkdir()
    baseline = destination / "baseline.txt"
    baseline.write_text("complete", encoding="utf-8")
    result = SatelliteValueResult(
        scores=pl.DataFrame({"feature_set": ["baseline"], "rmse": [1.0]}),
        deltas=pl.DataFrame({"comparison": ["baseline_aod_minus_baseline"]}),
        coverage=pl.DataFrame({"station_month_rows": [1]}),
        station_folds=pl.DataFrame({"station_name": ["富貴角"], "station_fold": [0]}),
        summary={"overall": {}, "evaluations": {}},
        manifest={"schema_version": 1, "complete": True},
    )
    real_replace = Path.replace
    replace_calls = 0

    def interrupt_after_first_replace(path: Path, target: Path) -> Path:
        nonlocal replace_calls
        replace_calls += 1
        moved = real_replace(path, target)
        if replace_calls == 1:
            raise KeyboardInterrupt
        return moved

    monkeypatch.setattr(Path, "replace", interrupt_after_first_replace)

    with pytest.raises(KeyboardInterrupt):
        write_satellite_value_result(result, destination=destination)

    assert baseline.read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".m8_satellite_value.staging-*"))
    assert not list(tmp_path.glob(".m8_satellite_value.backup-*"))


def test_stale_swap_siblings_are_recovered_before_the_next_write(tmp_path: Path) -> None:
    destination = tmp_path / "m8_satellite_value"
    destination.mkdir()
    (destination / "baseline.txt").write_text("complete", encoding="utf-8")
    interrupted_backup = destination.with_name(".m8_satellite_value.backup-interrupted")
    destination.replace(interrupted_backup)
    interrupted_stage = destination.with_name(".m8_satellite_value.staging-interrupted")
    interrupted_stage.mkdir()
    (interrupted_stage / "partial.txt").write_text("partial", encoding="utf-8")
    result = SatelliteValueResult(
        scores=pl.DataFrame({"feature_set": ["baseline"], "rmse": [1.0]}),
        deltas=pl.DataFrame({"comparison": ["baseline_aod_minus_baseline"]}),
        coverage=pl.DataFrame({"station_month_rows": [1]}),
        station_folds=pl.DataFrame({"station_name": ["富貴角"], "station_fold": [0]}),
        summary={"overall": {}, "evaluations": {}},
        manifest={"schema_version": 1, "complete": True},
    )

    written = write_satellite_value_result(result, destination=destination)

    assert written["manifest"].is_file()
    assert not interrupted_backup.exists()
    assert not interrupted_stage.exists()


def test_a_failed_write_after_stale_recovery_keeps_the_previous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "m8_satellite_value"
    destination.mkdir()
    baseline = destination / "baseline.txt"
    baseline.write_text("complete", encoding="utf-8")
    interrupted_backup = destination.with_name(".m8_satellite_value.backup-interrupted")
    destination.replace(interrupted_backup)
    interrupted_stage = destination.with_name(".m8_satellite_value.staging-interrupted")
    interrupted_stage.mkdir()
    result = SatelliteValueResult(
        scores=pl.DataFrame({"feature_set": ["baseline"], "rmse": [1.0]}),
        deltas=pl.DataFrame({"comparison": ["baseline_aod_minus_baseline"]}),
        coverage=pl.DataFrame({"station_month_rows": [1]}),
        station_folds=pl.DataFrame({"station_name": ["富貴角"], "station_fold": [0]}),
        summary={"overall": {}, "evaluations": {}},
        manifest={"schema_version": 1, "complete": True},
    )
    monkeypatch.setattr(
        satellite_value,
        "_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected write failure")),
    )

    with pytest.raises(OSError, match="injected write failure"):
        write_satellite_value_result(result, destination=destination)

    assert baseline.read_text(encoding="utf-8") == "complete"
    assert not interrupted_backup.exists()
    assert not interrupted_stage.exists()
    assert not list(tmp_path.glob(".m8_satellite_value.staging-*"))


def test_the_runner_binds_the_generation_raw_inputs_and_station_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = station_inventory_generation(_inventory()).sha256
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    station_path = tmp_path / "outputs" / "qc" / "stations.parquet"
    station_path.parent.mkdir(parents=True)
    _inventory().write_parquet(station_path)
    ground_path = tmp_path / "processed" / "monthly" / "monthly.parquet"
    s5p_dir = tmp_path / "interim" / "satellite" / "source"
    maiac_dir = tmp_path / "interim" / "maiac" / "source"
    ground_path.parent.mkdir(parents=True)
    s5p_dir.mkdir(parents=True)
    maiac_dir.mkdir(parents=True)
    ground_path.write_bytes(b"ground")
    (s5p_dir / "values.parquet").write_bytes(b"s5p")
    (maiac_dir / "values.parquet").write_bytes(b"maiac")
    association = _association(_panel(), generation=generation)
    association.manifest["upstream"] = {
        "ground": {
            "path": "processed/monthly/monthly.parquet",
            "sha256": sha256(b"ground").hexdigest(),
        },
        "s5p": {
            "path": "interim/satellite/source",
            "files": {"values.parquet": sha256(b"s5p").hexdigest()},
        },
        "maiac": {
            "path": "interim/maiac/source",
            "files": {"values.parquet": sha256(b"maiac").hexdigest()},
        },
    }
    monkeypatch.setattr(
        satellite_value,
        "run_satellite_association",
        lambda *, year, generation: association,
    )

    def perfect_fit(
        _train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        truth = test["PM2.5"].to_numpy()
        return truth, truth.copy()

    monkeypatch.setattr(satellite_value, "_fit_predict", perfect_fit)

    result = run_satellite_value(
        data_root=tmp_path,
        generation_sha256=generation,
        config=_config(),
        generated_at="2026-08-11T21:00:00+00:00",
    )

    assert result.manifest["complete"] is True
    assert result.manifest["inventory_generation_sha256"] == generation
    assert result.manifest["common_complete_rows"] == 16
    assert result.manifest["score_rows"] == 70
    assert result.manifest["delta_rows"] == 56
    assert len(result.manifest["input_files"]) == 4
    assert all(len(item["sha256"]) == 64 for item in result.manifest["input_files"])
    assert result.manifest["limitations"] == [
        "descriptive held-out prediction within 2025, not causal attribution",
        "not a satellite PM2.5 calibration product or fused concentration field",
        "held-quarter transfer is seasonal blocking, not future-year forecasting",
        "not a replacement for M4 meteorological normalisation",
    ]


def test_the_runner_rejects_a_station_inventory_from_another_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_generation = "a" * 64
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    station_path = tmp_path / "outputs" / "qc" / "stations.parquet"
    station_path.parent.mkdir(parents=True)
    _inventory().write_parquet(station_path)
    association = _association(_panel(), generation=requested_generation)
    monkeypatch.setattr(
        satellite_value,
        "run_satellite_association",
        lambda *, year, generation: association,
    )

    with pytest.raises(RuntimeError, match="station inventory generation"):
        run_satellite_value(
            data_root=tmp_path,
            generation_sha256=requested_generation,
            config=_config(),
        )


def test_the_runner_rejects_an_input_that_changes_during_fitting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = station_inventory_generation(_inventory()).sha256
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    station_path = tmp_path / "outputs" / "qc" / "stations.parquet"
    station_path.parent.mkdir(parents=True)
    _inventory().write_parquet(station_path)
    ground_path = tmp_path / "ground.parquet"
    s5p_path = tmp_path / "s5p.parquet"
    maiac_path = tmp_path / "maiac.parquet"
    ground_path.write_bytes(b"ground")
    s5p_path.write_bytes(b"s5p")
    maiac_path.write_bytes(b"maiac")
    association = _association(_panel(), generation=generation)
    association.manifest["upstream"] = {
        "ground": {"path": "ground.parquet", "sha256": sha256(b"ground").hexdigest()},
        "s5p": {"path": ".", "files": {"s5p.parquet": sha256(b"s5p").hexdigest()}},
        "maiac": {
            "path": ".",
            "files": {"maiac.parquet": sha256(b"maiac").hexdigest()},
        },
    }
    monkeypatch.setattr(
        satellite_value,
        "run_satellite_association",
        lambda *, year, generation: association,
    )

    def mutating_fit(
        _train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        ground_path.write_bytes(b"changed")
        truth = test["PM2.5"].to_numpy()
        return truth, truth.copy()

    monkeypatch.setattr(satellite_value, "_fit_predict", mutating_fit)

    with pytest.raises(RuntimeError, match="input changed"):
        run_satellite_value(
            data_root=tmp_path,
            generation_sha256=generation,
            config=_config(),
        )


def test_the_runner_cannot_hash_a_different_data_root_than_the_one_it_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "configured"
    passed = tmp_path / "passed"
    configured.mkdir()
    passed.mkdir()
    monkeypatch.setenv("TWAIR_DATA_DIR", str(configured))
    monkeypatch.setattr(
        satellite_value,
        "run_satellite_association",
        lambda **_kwargs: pytest.fail("association read before the root mismatch was rejected"),
    )

    with pytest.raises(RuntimeError, match="configured data root"):
        run_satellite_value(
            data_root=passed,
            generation_sha256="a" * 64,
            config=_config(),
        )
