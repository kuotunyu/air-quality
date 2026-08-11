"""ERA5 value-add inputs keep local time, nulls, and paired rows explicit."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from twair.analysis import era5_value
from twair.analysis.era5_value import (
    ERA5_DERIVED_FEATURES,
    ERA5_VALUE_FEATURE_SETS,
    LOCAL_WEATHER_FEATURES,
    TEMPORAL_VALUE_FEATURES,
    Era5ValueResult,
    ModelConfig,
    assemble_local_era5_year,
    derive_era5_features,
    evaluate_paired_models,
    explicit_time_splits,
    load_era5_value_config,
    load_local_era5_year,
    paired_metric_deltas,
    prepare_paired_rows,
    run_era5_value,
    station_scope,
    summarise_metric_deltas,
    write_era5_value_result,
)
from twair.features.met import add_wind_features
from twair.features.temporal import add_temporal_features
from twair.ingest.era5 import Era5Result

ERA5_SOURCE_COLUMNS = (
    "blh_m",
    "u10_m_s",
    "v10_m_s",
    "t2m_k",
    "d2m_k",
    "sp_pa",
)


def _era5_hours(start: datetime, end: datetime) -> pl.DataFrame:
    times = pl.datetime_range(start, end, interval="1h", closed="left", eager=True)
    rows = times.len()
    return pl.DataFrame(
        {
            "station_name": ["萬里"] * rows,
            "ts_utc": times,
            "grid_lat": [25.25] * rows,
            "grid_lon": [121.75] * rows,
            "grid_distance_km": [7.5] * rows,
            "blh_m": [500.0] * rows,
            "u10_m_s": [3.0] * rows,
            "v10_m_s": [4.0] * rows,
            "t2m_k": [293.15] * rows,
            "d2m_k": [283.15] * rows,
            "sp_pa": [100_000.0] * rows,
        }
    )


def _local_2025_source_frames() -> tuple[pl.DataFrame, pl.DataFrame]:
    december_2024 = _era5_hours(
        datetime(2024, 12, 1, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
    )
    utc_2025 = _era5_hours(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    return december_2024, utc_2025


def test_a_local_calendar_year_uses_the_last_eight_hours_of_the_prior_utc_year() -> None:
    frame = assemble_local_era5_year(
        _local_2025_source_frames(),
        year=2025,
        expected_stations=("萬里",),
    )

    assert frame.height == 8_760
    assert frame["ts_local"].min() == datetime(2025, 1, 1)
    assert frame["ts_local"].max() == datetime(2025, 12, 31, 23)
    assert frame.filter(pl.col("ts_utc").dt.year() == 2024).height == 8
    assert frame.filter(pl.col("ts_utc").dt.year() == 2025).height == 8_752
    assert frame.unique(["station_name", "ts_local"]).height == frame.height


def test_a_missing_local_calendar_hour_is_reported_instead_of_filled() -> None:
    prior, current = _local_2025_source_frames()
    current = current.filter(pl.col("ts_utc") != datetime(2025, 6, 1, tzinfo=UTC))

    with pytest.raises(RuntimeError, match="complete local-calendar hours"):
        assemble_local_era5_year(
            (prior, current),
            year=2025,
            expected_stations=("萬里",),
        )


def test_a_duplicate_station_hour_is_rejected_before_the_calendar_filter() -> None:
    prior, current = _local_2025_source_frames()
    current = pl.concat([current, current.head(1)])

    with pytest.raises(RuntimeError, match="duplicated"):
        assemble_local_era5_year(
            (prior, current),
            year=2025,
            expected_stations=("萬里",),
        )


def test_the_expected_station_set_is_not_inferred_from_an_incomplete_source() -> None:
    with pytest.raises(RuntimeError, match="station set"):
        assemble_local_era5_year(
            _local_2025_source_frames(),
            year=2025,
            expected_stations=("萬里", "板橋"),
        )


def test_era5_features_preserve_source_values_and_convert_units() -> None:
    frame = _era5_hours(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 1, 1, tzinfo=UTC),
    )
    featured = derive_era5_features(frame)
    row = featured.row(0, named=True)

    assert set(ERA5_SOURCE_COLUMNS).issubset(featured.columns)
    assert set(ERA5_DERIVED_FEATURES).issubset(featured.columns)
    assert row["era5_blh_m"] == 500.0
    assert row["era5_u10_m_s"] == 3.0
    assert row["era5_v10_m_s"] == 4.0
    assert row["era5_wind_speed_m_s"] == pytest.approx(5.0)
    assert row["era5_t2m_c"] == pytest.approx(20.0)
    assert row["era5_sp_hpa"] == pytest.approx(1_000.0)
    assert row["era5_rh_pct"] == pytest.approx(52.54, abs=0.02)


@pytest.mark.parametrize("column", ERA5_SOURCE_COLUMNS)
def test_a_source_null_stays_null_in_every_derived_quantity_that_uses_it(column: str) -> None:
    frame = _era5_hours(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 1, 1, tzinfo=UTC),
    ).with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    featured = derive_era5_features(frame)
    row = featured.row(0, named=True)

    assert row[column] is None
    affected = {
        "blh_m": ("era5_blh_m",),
        "u10_m_s": ("era5_u10_m_s", "era5_wind_speed_m_s"),
        "v10_m_s": ("era5_v10_m_s", "era5_wind_speed_m_s"),
        "t2m_k": ("era5_t2m_c", "era5_rh_pct"),
        "d2m_k": ("era5_rh_pct",),
        "sp_pa": ("era5_sp_hpa",),
    }[column]
    assert all(row[name] is None for name in affected)


def test_a_non_finite_source_value_is_rejected_instead_of_coerced() -> None:
    frame = _era5_hours(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 1, 1, tzinfo=UTC),
    ).with_columns(pl.lit(float("inf")).alias("blh_m"))

    with pytest.raises(RuntimeError, match="non-finite"):
        derive_era5_features(frame)


def _write_loader_inputs(
    root: Path,
    *,
    generation: str,
) -> dict[Path, Era5Result]:
    results: dict[Path, Era5Result] = {}
    prior, current = _local_2025_source_frames()
    for year, months, values in (
        (2024, [12], prior),
        (2025, list(range(1, 13)), current),
    ):
        destination = root / "interim" / "era5" / "generations" / generation / f"year={year}"
        destination.mkdir(parents=True)
        manifest = {
            "year": year,
            "months": months,
            "stations_with_coordinates": 1,
            "inventory_generation_sha256": generation,
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        values.write_parquet(destination / "era5_station_hour.parquet")
        results[destination] = Era5Result(
            values=values,
            coverage=pl.DataFrame(),
            manifest=manifest,
        )
    return results


def test_the_filesystem_loader_hashes_the_exact_values_and_manifests_it_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = "a" * 64
    results = _write_loader_inputs(tmp_path, generation=generation)
    monkeypatch.setattr(
        "twair.analysis.era5_value.read_era5_result",
        lambda destination: results[destination],
    )

    loaded = load_local_era5_year(tmp_path, year=2025, generation_sha256=generation)

    assert loaded.inventory_generation_sha256 == generation
    assert loaded.values.height == 8_760
    assert [item.path.name for item in loaded.input_files] == [
        "manifest.json",
        "era5_station_hour.parquet",
        "manifest.json",
        "era5_station_hour.parquet",
    ]
    assert all(item.bytes > 0 and len(item.sha256) == 64 for item in loaded.input_files)


def test_a_generation_mismatch_is_rejected_even_when_a_reader_returns_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = "a" * 64
    results = _write_loader_inputs(tmp_path, generation=generation)
    wrong = dict(next(iter(results.values())).manifest)
    wrong["inventory_generation_sha256"] = "b" * 64
    first = next(iter(results))
    results[first] = Era5Result(
        values=results[first].values,
        coverage=pl.DataFrame(),
        manifest=wrong,
    )
    monkeypatch.setattr(
        "twair.analysis.era5_value.read_era5_result",
        lambda destination: results[destination],
    )

    with pytest.raises(RuntimeError, match="inventory generation"):
        load_local_era5_year(tmp_path, year=2025, generation_sha256=generation)


def test_an_input_rewrite_during_the_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = "a" * 64
    results = _write_loader_inputs(tmp_path, generation=generation)
    changed = False

    def _reader(destination: Path) -> Era5Result:
        nonlocal changed
        result = results[destination]
        if not changed:
            changed = True
            path = destination / "era5_station_hour.parquet"
            path.write_bytes(path.read_bytes() + b"changed")
        return result

    monkeypatch.setattr("twair.analysis.era5_value.read_era5_result", _reader)

    with pytest.raises(RuntimeError, match="changed while it was being read"):
        load_local_era5_year(tmp_path, year=2025, generation_sha256=generation)


def test_the_shipped_design_uses_three_expanding_quarters_and_one_cpu_thread() -> None:
    config = load_era5_value_config()

    assert config.year == 2025
    assert [fold.name for fold in config.folds] == ["q2", "q3", "q4"]
    assert [fold.train_start for fold in config.folds] == [datetime(2025, 1, 1)] * 3
    assert [fold.train_end for fold in config.folds] == [
        datetime(2025, 4, 1),
        datetime(2025, 7, 1),
        datetime(2025, 10, 1),
    ]
    assert [fold.test_start for fold in config.folds] == [
        datetime(2025, 4, 1),
        datetime(2025, 7, 1),
        datetime(2025, 10, 1),
    ]
    assert [fold.test_end for fold in config.folds] == [
        datetime(2025, 7, 1),
        datetime(2025, 10, 1),
        datetime(2026, 1, 1),
    ]
    assert config.model.n_jobs == 1
    assert config.pilot_stations == ("富貴角", "馬公", "忠明", "前金", "潮州", "埔里")


def _local_value_rows() -> pl.DataFrame:
    frame = pl.DataFrame(
        {
            "station_name": ["萬里"] * 4,
            "ts_local": [
                datetime(2025, 1, 1),
                datetime(2025, 1, 1, 1),
                datetime(2025, 1, 1, 2),
                datetime(2025, 1, 1, 3),
            ],
            "PM2.5": [10.0, 11.0, 12.0, 13.0],
            "AMB_TEMP": [20.0] * 4,
            "RH": [60.0, None, 62.0, 63.0],
            "RAINFALL": [0.0] * 4,
            "WS_HR": [2.0] * 4,
            "WD_HR": [0.0, 90.0, 180.0, 270.0],
        }
    )
    return add_temporal_features(add_wind_features(frame))


def _era5_value_rows() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": ["萬里", "萬里", "萬里"],
            "ts_local": [
                datetime(2025, 1, 1),
                datetime(2025, 1, 1, 1),
                datetime(2025, 1, 1, 3),
            ],
            "era5_blh_m": [500.0, 510.0, 530.0],
            "era5_u10_m_s": [3.0] * 3,
            "era5_v10_m_s": [4.0] * 3,
            "era5_wind_speed_m_s": [5.0] * 3,
            "era5_t2m_c": [20.0] * 3,
            "era5_rh_pct": [52.5, 53.0, None],
            "era5_sp_hpa": [1_000.0] * 3,
        }
    )


def test_paired_rows_measure_each_missingness_source_and_keep_only_the_intersection() -> None:
    paired = prepare_paired_rows(_local_value_rows(), _era5_value_rows())

    assert paired.values.select("station_name", "ts_local").to_dicts() == [
        {"station_name": "萬里", "ts_local": datetime(2025, 1, 1)}
    ]
    assert paired.coverage.row(0, named=True) == {
        "station_name": "萬里",
        "target_rows": 4,
        "era5_join_missing": 1,
        "local_feature_incomplete": 1,
        "era5_feature_incomplete": 2,
        "temporal_feature_incomplete": 0,
        "paired_rows": 1,
    }


def test_every_feature_set_is_scored_from_the_same_paired_row_keys() -> None:
    paired = prepare_paired_rows(_local_value_rows(), _era5_value_rows())
    keys = paired.values.select("station_name", "ts_local")

    assert set(ERA5_VALUE_FEATURE_SETS) == {
        "temporal_only",
        "local_weather",
        "era5_weather",
        "combined",
    }
    for features in ERA5_VALUE_FEATURE_SETS.values():
        assert paired.values.select(*features, "PM2.5").height == keys.height
        assert paired.values.select(*features, "PM2.5").null_count().sum_horizontal().item() == 0
    assert set(LOCAL_WEATHER_FEATURES).issubset(ERA5_VALUE_FEATURE_SETS["combined"])
    assert set(ERA5_DERIVED_FEATURES).issubset(ERA5_VALUE_FEATURE_SETS["combined"])
    assert set(TEMPORAL_VALUE_FEATURES).issubset(ERA5_VALUE_FEATURE_SETS["combined"])


def test_explicit_splits_never_put_one_timestamp_on_both_sides() -> None:
    config = load_era5_value_config()
    timestamps = [
        datetime(2025, 1, 1),
        datetime(2025, 3, 31, 23),
        datetime(2025, 4, 1),
        datetime(2025, 6, 30, 23),
        datetime(2025, 7, 1),
        datetime(2025, 9, 30, 23),
        datetime(2025, 10, 1),
        datetime(2025, 12, 31, 23),
    ]
    frame = pl.DataFrame(
        {
            "station_name": [station for timestamp in timestamps for station in ("甲", "乙")],
            "ts_local": [timestamp for timestamp in timestamps for _ in ("甲", "乙")],
            "PM2.5": list(range(len(timestamps) * 2)),
        }
    )

    splits = explicit_time_splits(frame, config.folds)

    assert [split.name for split in splits] == ["q2", "q3", "q4"]
    for split in splits:
        train_times = set(split.train["ts_local"].to_list())
        test_times = set(split.test["ts_local"].to_list())
        assert train_times.isdisjoint(test_times)
        assert max(train_times) < min(test_times)


def _complete_model_rows() -> pl.DataFrame:
    timestamps = pl.datetime_range(
        datetime(2025, 1, 1),
        datetime(2026, 1, 1),
        interval="1d",
        closed="left",
        eager=True,
    )
    rows = timestamps.len()
    base = pl.DataFrame(
        {
            "station_name": ["萬里"] * rows,
            "ts_local": timestamps,
            "PM2.5": [float(index % 40) for index in range(rows)],
            "AMB_TEMP": [20.0] * rows,
            "RH": [60.0] * rows,
            "RAINFALL": [0.0] * rows,
            "WS_HR": [2.0] * rows,
            "WD_HR": [0.0] * rows,
        }
    )
    local = add_temporal_features(add_wind_features(base))
    era5 = pl.DataFrame(
        {
            "station_name": ["萬里"] * rows,
            "ts_local": timestamps,
            **{name: [float(index + 1)] * rows for index, name in enumerate(ERA5_DERIVED_FEATURES)},
        }
    )
    return prepare_paired_rows(local, era5).values


def test_all_four_models_receive_identical_station_hour_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_era5_value_config()
    observed_keys: list[tuple[tuple[object, ...], tuple[object, ...]]] = []

    def _fit_predict(
        train: pl.DataFrame,
        test: pl.DataFrame,
        features: tuple[str, ...],
        model: ModelConfig,
    ) -> tuple[object, object]:
        assert features in ERA5_VALUE_FEATURE_SETS.values()
        assert model.n_jobs == 1
        observed_keys.append(
            (
                tuple(train.select("station_name", "ts_local").iter_rows()),
                tuple(test.select("station_name", "ts_local").iter_rows()),
            )
        )
        truth = test["PM2.5"].to_numpy()
        return truth, truth

    monkeypatch.setattr(era5_value, "_fit_predict", _fit_predict)

    scores = evaluate_paired_models(_complete_model_rows(), config)

    assert scores.height == 3 * 4
    assert scores["n_train"].min() > 0
    assert scores["n_test"].min() > 0
    assert scores["rmse"].unique().to_list() == [0.0]
    for offset in range(0, len(observed_keys), 4):
        assert len(set(observed_keys[offset : offset + 4])) == 1


def test_paired_metric_deltas_make_improvement_directions_explicit() -> None:
    scores = pl.DataFrame(
        {
            "station_name": ["萬里"] * 4,
            "fold": ["q2"] * 4,
            "feature_set": ["temporal_only", "local_weather", "era5_weather", "combined"],
            "n_train": [100] * 4,
            "n_test": [50] * 4,
            "rmse": [4.0, 3.0, 3.5, 2.0],
            "mae": [3.0, 2.0, 2.5, 1.0],
            "r2": [0.1, 0.2, 0.15, 0.4],
            "fit_seconds": [0.1] * 4,
        }
    )

    deltas = paired_metric_deltas(scores)
    combined = deltas.filter(pl.col("comparison") == "combined_minus_local").row(0, named=True)

    assert deltas.height == 3
    assert combined["rmse_delta"] == -1.0
    assert combined["mae_delta"] == -1.0
    assert combined["r2_delta"] == pytest.approx(0.2)
    assert combined["rmse_improved"] is True
    assert combined["r2_improved"] is True


def test_the_summary_reports_wins_losses_mixed_results_and_the_worst_fold() -> None:
    deltas = pl.DataFrame(
        {
            "station_name": ["甲", "乙", "丙"],
            "fold": ["q2", "q3", "q4"],
            "comparison": ["combined_minus_local"] * 3,
            "candidate": ["combined"] * 3,
            "reference": ["local_weather"] * 3,
            "n_train": [100, 200, 300],
            "n_test": [50, 50, 50],
            "rmse_delta": [-1.0, 1.0, -0.5],
            "mae_delta": [-0.5, 0.5, 0.1],
            "r2_delta": [0.2, -0.1, -0.01],
            "rmse_improved": [True, False, True],
            "r2_improved": [True, False, False],
        }
    )

    summary = summarise_metric_deltas(deltas)
    result = summary["comparisons"]["combined_minus_local"]

    assert result["station_folds"] == 3
    assert result["stations"] == 3
    assert result["both_improved"] == 1
    assert result["both_worse"] == 1
    assert result["mixed"] == 1
    assert result["median_rmse_delta"] == -0.5
    assert result["median_r2_delta"] == pytest.approx(-0.01)
    assert result["worst_fold_by_median_rmse_delta"] == "q3"


def test_the_writer_atomically_replaces_a_stale_directory_with_exact_outputs(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "m8_era5_value"
    destination.mkdir()
    (destination / "stale.txt").write_text("partial", encoding="utf-8")
    result = Era5ValueResult(
        scores=pl.DataFrame({"station_name": ["萬里"], "rmse": [1.0]}),
        deltas=pl.DataFrame({"station_name": ["萬里"], "rmse_delta": [-0.1]}),
        coverage=pl.DataFrame({"station_name": ["萬里"], "paired_rows": [100]}),
        summary={"comparisons": {}},
        manifest={"schema_version": 1, "complete": True},
    )

    written = write_era5_value_result(result, destination=destination)

    assert {path.name for path in destination.iterdir()} == {
        "coverage.parquet",
        "manifest.json",
        "paired_deltas.parquet",
        "scores.parquet",
        "summary.json",
    }
    assert set(written) == {"scores", "paired_deltas", "coverage", "summary", "manifest"}
    assert pl.read_parquet(written["scores"]).equals(result.scores)
    assert json.loads(written["summary"].read_text(encoding="utf-8")) == result.summary
    assert json.loads(written["manifest"].read_text(encoding="utf-8")) == result.manifest


def _runner_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, Path]:
    generation = "a" * 64
    results = _write_loader_inputs(tmp_path, generation=generation)
    monkeypatch.setattr(
        "twair.analysis.era5_value.read_era5_result",
        lambda destination: results[destination],
    )
    observation = (
        tmp_path / "processed" / "observations" / "year=2025" / "month=01" / "part-0.parquet"
    )
    observation.parent.mkdir(parents=True)
    observation.write_bytes(b"canonical observation bytes")
    local = _complete_model_rows().drop(*ERA5_DERIVED_FEATURES)
    monkeypatch.setattr(era5_value, "build_modelling_frame", lambda *_args, **_kwargs: local)

    def _perfect_fit(
        train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[object, object]:
        truth = test["PM2.5"].to_numpy()
        return truth, truth

    monkeypatch.setattr(era5_value, "_fit_predict", _perfect_fit)
    return generation, observation


def test_the_runner_binds_results_to_exact_observation_and_era5_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation, observation = _runner_inputs(tmp_path, monkeypatch)

    result = run_era5_value(
        data_root=tmp_path,
        generation_sha256=generation,
        pilot=False,
        generated_at="2026-08-11T12:00:00+00:00",
    )

    assert result.scores.height == 12
    assert result.deltas.height == 9
    assert result.coverage["paired_rows"].sum() == 365
    assert result.manifest["inventory_generation_sha256"] == generation
    assert result.manifest["generated_at"] == "2026-08-11T12:00:00+00:00"
    assert result.manifest["station_scope"] == {
        "candidate_stations": 1,
        "target_available_stations": 1,
        "analyzed_stations": 1,
        "excluded_stations": [],
    }
    inputs = result.manifest["input_files"]
    assert any(
        item["path"].endswith("processed/observations/year=2025/month=01/part-0.parquet")
        for item in inputs
    )
    recorded = next(item for item in inputs if item["path"].endswith("part-0.parquet"))
    assert recorded["bytes"] == observation.stat().st_size
    assert len(recorded["sha256"]) == 64


def test_the_station_scope_names_every_station_excluded_before_modelling() -> None:
    assert station_scope(
        candidate_stations=("三重", "淡水", "萬里", "陽明"),
        target_stations=("淡水", "萬里", "陽明"),
        analyzed_stations=("萬里", "陽明"),
    ) == {
        "candidate_stations": 4,
        "target_available_stations": 3,
        "analyzed_stations": 2,
        "excluded_stations": [
            {
                "station_name": "三重",
                "reason": "no_pm25_target_rows_in_analysis_year",
            },
            {
                "station_name": "淡水",
                "reason": "no_common_complete_rows_for_all_feature_sets",
            },
        ],
    }


def test_the_runner_refuses_to_publish_after_a_canonical_input_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation, observation = _runner_inputs(tmp_path, monkeypatch)
    changed = False

    def _mutating_fit(
        _train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[object, object]:
        nonlocal changed
        if not changed:
            changed = True
            observation.write_bytes(b"rewritten while models were fitting")
        truth = test["PM2.5"].to_numpy()
        return truth, truth

    monkeypatch.setattr(era5_value, "_fit_predict", _mutating_fit)

    with pytest.raises(RuntimeError, match="changed while models were fitting"):
        run_era5_value(
            data_root=tmp_path,
            generation_sha256=generation,
            pilot=False,
        )
