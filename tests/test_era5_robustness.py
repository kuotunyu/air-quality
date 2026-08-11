from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import polars as pl
import pytest

import twair.analysis.era5_robustness as robustness
from twair.analysis.era5_robustness import (
    Era5RobustnessConfig,
    Era5RobustnessResult,
    annual_expanding_folds,
    assign_station_folds,
    evaluate_same_station_transfer,
    evaluate_station_fold_transfer,
    load_era5_robustness_config,
    robustness_metric_deltas,
    run_era5_robustness,
    summarise_robustness_deltas,
    write_era5_robustness_result,
)
from twair.analysis.era5_value import ERA5_VALUE_FEATURE_SETS, InputFile, ModelConfig, PairedRows
from twair.config import ConfigError


def _model() -> ModelConfig:
    return ModelConfig(
        n_estimators=10,
        learning_rate=0.05,
        num_leaves=7,
        min_child_samples=2,
        n_jobs=1,
        seed=20260811,
    )


def _complete_year(year: int, stations: tuple[str, ...]) -> pl.DataFrame:
    timestamps = pl.datetime_range(
        datetime(year, 1, 1),
        datetime(year + 1, 1, 1),
        interval="1d",
        closed="left",
        eager=True,
    )
    rows: list[dict[str, object]] = []
    all_features = tuple(
        dict.fromkeys(name for features in ERA5_VALUE_FEATURE_SETS.values() for name in features)
    )
    for station_index, station in enumerate(stations):
        for day_index, timestamp in enumerate(timestamps):
            row: dict[str, object] = {
                "station_name": station,
                "ts_local": timestamp,
                "PM2.5": float((station_index + day_index) % 40),
            }
            row.update(
                {
                    name: float(feature_index + station_index + 1)
                    for feature_index, name in enumerate(all_features)
                }
            )
            rows.append(row)
    return pl.DataFrame(rows).sort("station_name", "ts_local")


def _perfect_fit(
    _train: pl.DataFrame,
    test: pl.DataFrame,
    _features: tuple[str, ...],
    _model: ModelConfig,
) -> tuple[object, object]:
    truth = test["PM2.5"].to_numpy()
    return truth, truth


def test_the_shipped_robustness_config_is_two_years_serial_and_has_fixed_station_folds() -> None:
    config = load_era5_robustness_config()

    assert config.years == (2024, 2025)
    assert config.station_folds == 10
    assert config.model.n_jobs == 1
    assert config.pilot_stations == ("富貴角", "馬公", "忠明", "前金", "潮州", "埔里")


def test_the_robustness_config_rejects_parallel_lightgbm() -> None:
    raw = {
        "analysis": {
            "years": [2024, 2025],
            "station_folds": 2,
            "pilot_stations": ["甲", "乙"],
            "model": {
                "n_estimators": 10,
                "learning_rate": 0.05,
                "num_leaves": 7,
                "min_child_samples": 2,
                "n_jobs": 2,
                "seed": 1,
            },
        }
    }

    with pytest.raises(ConfigError, match="n_jobs=1"):
        load_era5_robustness_config(raw)


@pytest.mark.parametrize("year", [2024, 2025])
def test_each_year_uses_the_same_three_expanding_local_calendar_quarters(year: int) -> None:
    folds = annual_expanding_folds(year)

    assert [fold.name for fold in folds] == ["q2", "q3", "q4"]
    assert [fold.train_start for fold in folds] == [datetime(year, 1, 1)] * 3
    assert [fold.train_end for fold in folds] == [
        datetime(year, 4, 1),
        datetime(year, 7, 1),
        datetime(year, 10, 1),
    ]
    assert [fold.test_end for fold in folds] == [
        datetime(year, 7, 1),
        datetime(year, 10, 1),
        datetime(year + 1, 1, 1),
    ]


def _station_inventory() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"],
            "airzone_official": ["北", "北", "北", "中", "中", "中", "南", "南", "南", "南"],
        }
    )


def test_station_folds_are_deterministic_balanced_and_hold_every_station_once() -> None:
    inventory = _station_inventory()

    first = assign_station_folds(inventory, fold_count=4)
    second = assign_station_folds(inventory.reverse(), fold_count=4)

    assert first.equals(second)
    assert first.height == inventory.height
    assert first["station_name"].n_unique() == inventory.height
    assert sorted(first["station_fold"].unique().to_list()) == [0, 1, 2, 3]
    counts = first.group_by("station_fold").len()["len"]
    assert counts.max() - counts.min() <= 1
    for zone in inventory["airzone_official"].unique().to_list():
        assert first.filter(pl.col("airzone_official") == zone)["station_fold"].n_unique() > 1


def test_a_station_without_an_official_airzone_remains_in_a_deterministic_fold() -> None:
    inventory = pl.DataFrame(
        {
            "station_name": ["甲", "萬里", "乙"],
            "airzone_official": ["北", None, "北"],
        }
    )

    first = assign_station_folds(inventory, fold_count=2)
    second = assign_station_folds(inventory.reverse(), fold_count=2)

    assert first.equals(second)
    assert first["station_name"].to_list() == ["乙", "甲", "萬里"]
    wanli = first.filter(pl.col("station_name") == "萬里").row(0, named=True)
    assert wanli["airzone_official"] is None
    assert wanli["station_fold"] in {0, 1}


@pytest.mark.parametrize(
    ("inventory", "message"),
    [
        (
            pl.DataFrame({"station_name": ["甲", None], "airzone_official": ["北", "北"]}),
            "name",
        ),
        (
            pl.DataFrame({"station_name": ["甲", "甲"], "airzone_official": ["北", "北"]}),
            "duplicated",
        ),
    ],
)
def test_station_fold_inputs_are_rejected_instead_of_repaired(
    inventory: pl.DataFrame,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        assign_station_folds(inventory, fold_count=2)


def test_same_station_transfer_is_strictly_forward_and_never_mixes_stations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    train = _complete_year(2024, ("甲", "乙"))
    test = _complete_year(2025, ("甲", "乙"))
    calls: list[tuple[set[str], set[str], datetime, datetime]] = []

    def observed_fit(
        train_rows: pl.DataFrame,
        test_rows: pl.DataFrame,
        features: tuple[str, ...],
        model: ModelConfig,
    ) -> tuple[object, object]:
        assert features in ERA5_VALUE_FEATURE_SETS.values()
        assert model.n_jobs == 1
        train_max = train_rows["ts_local"].max()
        test_min = test_rows["ts_local"].min()
        assert isinstance(train_max, datetime)
        assert isinstance(test_min, datetime)
        calls.append(
            (
                set(train_rows["station_name"].to_list()),
                set(test_rows["station_name"].to_list()),
                train_max,
                test_min,
            )
        )
        return _perfect_fit(train_rows, test_rows, features, model)

    monkeypatch.setattr(robustness, "_fit_predict", observed_fit)

    scores = evaluate_same_station_transfer(train, test, model=_model())

    assert scores.height == 2 * 4
    assert scores["evaluation"].unique().to_list() == ["temporal_transfer"]
    assert scores["train_year"].unique().to_list() == [2024]
    assert scores["test_year"].unique().to_list() == [2025]
    assert all(train_stations == test_stations for train_stations, test_stations, _, _ in calls)
    assert all(len(train_stations) == 1 for train_stations, _, _, _ in calls)
    assert all(train_max < test_min for _, _, train_max, test_min in calls)


@pytest.mark.parametrize(
    ("train_year", "test_year", "evaluation"),
    [
        (2025, 2025, "spatial_transfer"),
        (2024, 2025, "spatiotemporal_transfer"),
    ],
)
def test_station_fold_transfer_never_trains_on_a_held_out_station_and_pairs_all_models(
    train_year: int,
    test_year: int,
    evaluation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stations = ("甲", "乙", "丙", "丁")
    train = _complete_year(train_year, stations)
    test = _complete_year(test_year, stations)
    membership = pl.DataFrame(
        {
            "station_name": list(stations),
            "airzone_official": ["北", "北", "南", "南"],
            "station_fold": [0, 1, 0, 1],
        }
    )
    calls: list[
        tuple[tuple[str, ...], tuple[str, ...], tuple[object, ...], tuple[object, ...]]
    ] = []

    def observed_fit(
        train_rows: pl.DataFrame,
        test_rows: pl.DataFrame,
        features: tuple[str, ...],
        model: ModelConfig,
    ) -> tuple[object, object]:
        assert features in ERA5_VALUE_FEATURE_SETS.values()
        calls.append(
            (
                tuple(sorted(train_rows["station_name"].unique().to_list())),
                tuple(sorted(test_rows["station_name"].unique().to_list())),
                tuple(train_rows.select("station_name", "ts_local").iter_rows()),
                tuple(test_rows.select("station_name", "ts_local").iter_rows()),
            )
        )
        return _perfect_fit(train_rows, test_rows, features, model)

    monkeypatch.setattr(robustness, "_fit_predict", observed_fit)

    scores = evaluate_station_fold_transfer(
        train,
        test,
        membership,
        model=_model(),
        evaluation=evaluation,
    )

    assert scores.height == 4 * 4
    assert set(scores["station_name"].to_list()) == set(stations)
    assert scores["evaluation"].unique().to_list() == [evaluation]
    assert len(calls) == 2 * 4
    for train_stations, test_stations, _, _ in calls:
        assert set(train_stations).isdisjoint(test_stations)
    for offset in range(0, len(calls), 4):
        assert len({item[2] for item in calls[offset : offset + 4]}) == 1
        assert len({item[3] for item in calls[offset : offset + 4]}) == 1
    if train_year < test_year:
        assert train["ts_local"].max() < test["ts_local"].min()


def test_station_fold_transfer_rejects_a_missing_or_duplicate_membership() -> None:
    frame = _complete_year(2025, ("甲", "乙"))
    duplicate = pl.DataFrame(
        {
            "station_name": ["甲", "甲"],
            "airzone_official": ["北", "北"],
            "station_fold": [0, 1],
        }
    )

    with pytest.raises(RuntimeError, match="membership"):
        evaluate_station_fold_transfer(
            frame,
            frame,
            duplicate,
            model=_model(),
            evaluation="spatial_transfer",
        )


def test_station_fold_transfer_rejects_predictions_for_different_test_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _complete_year(2025, ("甲", "乙"))
    membership = pl.DataFrame(
        {
            "station_name": ["甲", "乙"],
            "airzone_official": ["北", "南"],
            "station_fold": [0, 1],
        }
    )

    def wrong_truth(
        _train: pl.DataFrame,
        test: pl.DataFrame,
        _features: tuple[str, ...],
        _model: ModelConfig,
    ) -> tuple[object, object]:
        truth = test["PM2.5"].to_numpy()
        return truth[::-1], truth

    monkeypatch.setattr(robustness, "_fit_predict", wrong_truth)

    with pytest.raises(RuntimeError, match="different test rows"):
        evaluate_station_fold_transfer(
            frame,
            frame,
            membership,
            model=_model(),
            evaluation="spatial_transfer",
        )


def test_robustness_deltas_keep_evaluation_and_station_fold_boundaries() -> None:
    scores = pl.DataFrame(
        {
            "evaluation": ["spatial_transfer"] * 4,
            "train_year": [2025] * 4,
            "test_year": [2025] * 4,
            "station_fold": [2] * 4,
            "station_name": ["萬里"] * 4,
            "fold": ["station_fold_02"] * 4,
            "feature_set": ["temporal_only", "local_weather", "era5_weather", "combined"],
            "n_train": [100] * 4,
            "n_test": [50] * 4,
            "rmse": [4.0, 3.0, 3.5, 2.0],
            "mae": [3.0, 2.0, 2.5, 1.0],
            "r2": [0.1, 0.2, 0.15, 0.4],
            "fit_seconds": [0.1] * 4,
        }
    )

    deltas = robustness_metric_deltas(scores)
    combined = deltas.filter(pl.col("comparison") == "combined_minus_local").row(0, named=True)
    summary = summarise_robustness_deltas(deltas)

    assert deltas.height == 3
    assert combined["evaluation"] == "spatial_transfer"
    assert combined["station_fold"] == 2
    assert combined["rmse_delta"] == -1.0
    assert combined["r2_delta"] == pytest.approx(0.2)
    evaluation = summary["evaluations"]["spatial_transfer"]
    assert evaluation["overall"]["combined_minus_local"]["both_improved"] == 1
    assert evaluation["year_pairs"]["2025_to_2025"]["combined_minus_local"]["both_improved"] == 1


def _fake_replication_scores(frame: pl.DataFrame, *, year: int) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for station in sorted(frame["station_name"].unique().to_list()):
        for feature_set in ERA5_VALUE_FEATURE_SETS:
            rows.append(
                {
                    "evaluation": "year_replication",
                    "train_year": year,
                    "test_year": year,
                    "station_fold": None,
                    "station_name": station,
                    "fold": "q2",
                    "feature_set": feature_set,
                    "n_train": 10,
                    "n_test": 5,
                    "rmse": 1.0,
                    "mae": 0.8,
                    "r2": 0.5,
                    "fit_seconds": 0.01,
                }
            )
    return pl.DataFrame(rows, schema_overrides={"station_fold": pl.Int64})


def test_the_runner_binds_both_years_station_inventory_and_all_evaluation_designs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = "a" * 64
    station_names = ("甲", "乙")
    station_path = tmp_path / "outputs" / "qc" / "stations.parquet"
    station_path.parent.mkdir(parents=True)
    _station_inventory().filter(pl.col("station_name").is_in(station_names)).write_parquet(
        station_path
    )
    frames = {year: _complete_year(year, station_names) for year in (2024, 2025)}
    sources: dict[int, InputFile] = {}
    for year in (2024, 2025):
        path = tmp_path / f"source-{year}.bin"
        payload = f"source {year}".encode()
        path.write_bytes(payload)
        sources[year] = InputFile(path=path, bytes=len(payload), sha256=sha256(payload).hexdigest())

    def paired_year(
        _data_root: Path,
        *,
        year: int,
        generation_sha256: str,
        stations: list[str] | None,
    ) -> tuple[PairedRows, tuple[InputFile, ...], tuple[str, ...], dict[str, object]]:
        assert generation_sha256 == generation
        assert stations is None
        coverage = pl.DataFrame(
            {
                "station_name": list(station_names),
                "target_rows": [frames[year].height // 2] * 2,
                "era5_join_missing": [0, 0],
                "local_feature_incomplete": [0, 0],
                "era5_feature_incomplete": [0, 0],
                "temporal_feature_incomplete": [0, 0],
                "paired_rows": [frames[year].height // 2] * 2,
            }
        )
        scope: dict[str, object] = {
            "candidate_stations": 2,
            "target_available_stations": 2,
            "analyzed_stations": 2,
            "excluded_stations": [],
        }
        return PairedRows(frames[year], coverage), (sources[year],), station_names, scope

    monkeypatch.setattr(robustness, "_paired_year", paired_year)
    monkeypatch.setattr(
        robustness,
        "_year_replication",
        lambda frame, *, year, config: _fake_replication_scores(frame, year=year),
    )
    monkeypatch.setattr(robustness, "_fit_predict", _perfect_fit)
    config = Era5RobustnessConfig(
        years=(2024, 2025),
        station_folds=2,
        pilot_stations=station_names,
        model=_model(),
    )

    result = run_era5_robustness(
        data_root=tmp_path,
        generation_sha256=generation,
        pilot=False,
        config=config,
        generated_at="2026-08-11T20:00:00+00:00",
    )

    assert result.manifest["complete"] is True
    assert result.manifest["years"] == [2024, 2025]
    assert result.manifest["common_stations"] == sorted(station_names)
    assert set(result.manifest["evaluations"]) == {
        "year_replication",
        "temporal_transfer",
        "spatial_transfer",
        "spatiotemporal_transfer",
    }
    assert result.manifest["paired_rows_by_year"] == {"2024": 732, "2025": 730}
    assert result.manifest["station_fold_method"] == (
        "airzone_sorted_round_robin_with_unclassified_stratum"
    )
    assert result.manifest["unclassified_airzone_station_count"] == 0
    assert len(result.manifest["input_files"]) == 3
    assert result.station_folds.height == 2
    assert result.coverage.height == 4
    assert result.scores["evaluation"].n_unique() == 4


def test_the_robustness_writer_atomically_replaces_stale_output(tmp_path: Path) -> None:
    destination = tmp_path / "m8_era5_robustness"
    destination.mkdir()
    (destination / "stale.txt").write_text("partial", encoding="utf-8")
    result = Era5RobustnessResult(
        scores=pl.DataFrame({"station_name": ["萬里"], "rmse": [1.0]}),
        deltas=pl.DataFrame({"station_name": ["萬里"], "rmse_delta": [-0.1]}),
        coverage=pl.DataFrame({"year": [2025], "station_name": ["萬里"]}),
        station_folds=pl.DataFrame({"station_name": ["萬里"], "station_fold": [0]}),
        summary={"evaluations": {}},
        manifest={"schema_version": 1, "complete": True},
    )

    written = write_era5_robustness_result(result, destination=destination)

    assert {path.name for path in destination.iterdir()} == {
        "coverage.parquet",
        "manifest.json",
        "paired_deltas.parquet",
        "scores.parquet",
        "station_folds.parquet",
        "summary.json",
    }
    assert set(written) == {
        "scores",
        "paired_deltas",
        "coverage",
        "station_folds",
        "summary",
        "manifest",
    }
    assert json.loads(written["manifest"].read_text(encoding="utf-8"))["complete"] is True
