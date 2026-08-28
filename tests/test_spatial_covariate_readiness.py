"""Contract tests for frozen spatial covariate readiness inputs."""

from __future__ import annotations

import copy
import json
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twair.analysis.spatial_covariate_readiness import (
    CovariateReadinessConfig,
    InputFile,
    load_frozen_inputs,
    load_spatial_covariate_readiness_config,
)
from twair.analysis.spatial_surface_baseline import SPATIAL_BASELINE_TABLE_SCHEMAS
from twair.config import ConfigError

BASELINE_GENERATION = "620b7ba088906611c191d0f371b5405f8096059cefc488306b6849b64588ef0f"
INVENTORY_GENERATION = "58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788"


def _config_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "analysis": {
            "years": [2024, 2025],
            "baseline_generation_sha256": "a" * 64,
            "station_inventory_generation_sha256": "b" * 64,
            "minimum_train_stations": 8,
        },
        "methods": {
            "comparator": "idw2",
            "candidates": ["covariate_gbm", "covariate_gbm_idw2"],
            "idw_power": 2.0,
            "minimum_distance_km": 0.1,
            "model": {
                "n_estimators": 200,
                "learning_rate": 0.05,
                "num_leaves": 31,
                "min_child_samples": 10,
                "n_jobs": 1,
                "seed": 20260811,
            },
        },
        "validation": {
            "evaluations": ["buffer_20km", "buffer_40km", "spatial_cluster"],
            "bootstrap_draws": 9999,
            "bootstrap_seed": 20260828,
        },
    }


def _config() -> CovariateReadinessConfig:
    payload = _config_payload()
    analysis = payload["analysis"]
    assert isinstance(analysis, dict)
    analysis["baseline_generation_sha256"] = BASELINE_GENERATION
    analysis["station_inventory_generation_sha256"] = INVENTORY_GENERATION
    return load_spatial_covariate_readiness_config(payload)


def test_shipped_config_pins_the_reviewed_covariate_contract() -> None:
    config = load_spatial_covariate_readiness_config()

    assert config.years == (2024, 2025)
    assert config.baseline_generation_sha256 == BASELINE_GENERATION
    assert config.station_inventory_generation_sha256 == INVENTORY_GENERATION
    assert config.minimum_train_stations == 8
    assert config.methods == ("idw2", "covariate_gbm", "covariate_gbm_idw2")
    assert config.comparator == "idw2"
    assert config.idw_power == 2.0
    assert config.minimum_distance_km == 0.1
    assert config.model.n_estimators == 200
    assert config.model.learning_rate == 0.05
    assert config.model.num_leaves == 31
    assert config.model.min_child_samples == 10
    assert config.model.n_jobs == 1
    assert config.model.seed == 20260811
    assert config.evaluations == ("buffer_20km", "buffer_40km", "spatial_cluster")
    assert config.bootstrap_draws == 9999
    assert config.bootstrap_seed == 20260828


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (
            lambda raw: raw.__setitem__("schema_version", 2),
            "spatial_covariate_readiness.schema_version",
        ),
        (
            lambda raw: raw["analysis"].__setitem__("years", [2025, 2024]),
            "spatial_covariate_readiness.analysis.years",
        ),
        (
            lambda raw: raw["analysis"].__setitem__("baseline_generation_sha256", "not-a-sha"),
            "spatial_covariate_readiness.analysis.baseline_generation_sha256",
        ),
        (
            lambda raw: raw["methods"].__setitem__(
                "candidates", ["covariate_gbm_idw2", "covariate_gbm"]
            ),
            "spatial_covariate_readiness.methods.candidates",
        ),
        (
            lambda raw: raw["methods"].__setitem__("comparator", "nearest"),
            "spatial_covariate_readiness.methods.comparator",
        ),
        (
            lambda raw: raw["methods"].__setitem__("minimum_distance_km", 0.0),
            "spatial_covariate_readiness.methods.minimum_distance_km",
        ),
        (
            lambda raw: raw["validation"].__setitem__("bootstrap_draws", 0),
            "spatial_covariate_readiness.validation.bootstrap_draws",
        ),
        (
            lambda raw: raw["methods"]["model"].__setitem__("n_jobs", 2),
            "spatial_covariate_readiness.methods.model.n_jobs",
        ),
        (
            lambda raw: raw["methods"]["model"].__setitem__("seed", 20260812),
            "spatial_covariate_readiness.methods.model.seed",
        ),
        (
            lambda raw: raw["validation"].__setitem__(
                "evaluations", ["buffer_20km", "buffer_20km", "spatial_cluster"]
            ),
            "spatial_covariate_readiness.validation.evaluations",
        ),
        (
            lambda raw: raw["validation"].__setitem__(
                "evaluations", ["buffer_20km", "buffer_40km", "spatial_regions"]
            ),
            "spatial_covariate_readiness.validation.evaluations",
        ),
    ],
)
def test_config_rejects_any_drift_with_its_exact_path(mutate: Any, path: str) -> None:
    raw = copy.deepcopy(_config_payload())
    mutate(raw)

    with pytest.raises(ConfigError, match=path):
        load_spatial_covariate_readiness_config(raw)


def _baseline_directory(root: Path) -> Path:
    return root / "outputs" / "spatial_surface_baseline" / "generations" / BASELINE_GENERATION


def _write_baseline_fixture(
    root: Path,
    *,
    station_count: int = 59,
    omitted_panel_key: tuple[str, date] | None = None,
    withheld_key: tuple[str, date] = ("新營", date(2025, 5, 1)),
    extra_withheld: tuple[str, date] | None = None,
) -> list[Path]:
    directory = _baseline_directory(root)
    directory.mkdir(parents=True)
    station_names = [*(f"station-{index:02d}" for index in range(station_count - 1)), "新營"]
    months = [date(year, month, 1) for year in (2024, 2025) for month in range(1, 13)]
    stations = pl.DataFrame(
        {
            "station_name": station_names,
            "station_type_official": ["一般站"] * station_count,
            "lon": [120.0 + index * 0.01 for index in range(station_count)],
            "lat": [23.0 + index * 0.01 for index in range(station_count)],
        },
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["stations"],
    )
    panel_rows: list[dict[str, object]] = []
    for station_index, station_name in enumerate(station_names):
        for month in months:
            if (station_name, month) == omitted_panel_key:
                continue
            withheld = (station_name, month) == withheld_key or (
                station_name,
                month,
            ) == extra_withheld
            panel_rows.append(
                {
                    "station_name": station_name,
                    "station_type_official": "一般站",
                    "lon": 120.0 + station_index * 0.01,
                    "lat": 23.0 + station_index * 0.01,
                    "month": month,
                    "pollutant": "PM2.5",
                    "mean": None if withheld else float(10 + station_index + month.month),
                    "meets_threshold": not withheld,
                    "target_state": "withheld" if withheld else "observed",
                }
            )
    panel = pl.DataFrame(
        panel_rows,
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["panel"],
    )
    support = pl.DataFrame(
        {
            "station_name": station_names,
            "nearest_station": [
                station_names[(index + 1) % station_count] for index in range(station_count)
            ],
            "nearest_station_km": [1.0] * station_count,
            "stations_within_20km": [station_count - 1] * station_count,
            "stations_within_40km": [station_count - 1] * station_count,
            "x_m": [100.0 + index for index in range(station_count)],
            "y_m": [300.0 + index for index in range(station_count)],
        },
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["support"],
    )
    fold_rows: list[dict[str, object]] = []
    for evaluation in ("buffer_20km", "buffer_40km", "spatial_cluster"):
        for row in panel.iter_rows(named=True):
            station_name = str(row["station_name"])
            target_state = str(row["target_state"])
            fold_rows.append(
                {
                    "evaluation": evaluation,
                    "fold_id": f"{evaluation}:{station_name}",
                    "year": row["month"].year,
                    "month": row["month"],
                    "target_station": station_name,
                    "target_cluster": station_names.index(station_name) % 5,
                    "target_state": target_state,
                    "observed": row["mean"],
                    "train_stations": [name for name in station_names if name != station_name],
                    "n_train": station_count - 1,
                    "fold_state": (
                        "eligible" if target_state == "observed" else "unscored_target_withheld"
                    ),
                    "fold_reason": None if target_state == "observed" else "target_withheld",
                }
            )
    folds = pl.DataFrame(fold_rows, schema=SPATIAL_BASELINE_TABLE_SCHEMAS["folds"])
    frames = {"stations": stations, "panel": panel, "support": support, "folds": folds}
    for name, frame in frames.items():
        frame.write_parquet(directory / f"{name}.parquet")
    member_paths = [directory / f"{name}.parquet" for name in frames]
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "complete": True,
                "generation_sha256": BASELINE_GENERATION,
                "inventory_generation_sha256": INVENTORY_GENERATION,
                "members": {
                    path.name: {"bytes": path.stat().st_size, "sha256": _identity(path).sha256}
                    for path in member_paths
                },
            }
        ),
        encoding="utf-8",
    )
    return [manifest_path, *(directory / f"{name}.parquet" for name in frames)]


def _write_external_inputs(root: Path) -> list[Path]:
    paths: list[Path] = []
    for year in (2023, 2024, 2025):
        path = (
            root
            / "interim"
            / "era5"
            / "generations"
            / INVENTORY_GENERATION
            / f"year={year}"
            / "era5_station_hour.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"era5-{year}".encode())
        paths.append(path)
    for year in (2024, 2025):
        path = (
            root
            / "outputs"
            / "m8_satellite"
            / "generations"
            / INVENTORY_GENERATION
            / f"year={year}"
            / "panel.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"m8-{year}".encode())
        paths.append(path)
    return paths


def _identity(path: Path) -> InputFile:
    payload = path.read_bytes()
    return InputFile(path=path, bytes=len(payload), sha256=sha256(payload).hexdigest())


def _refresh_manifest_member_identity(root: Path, member: str) -> None:
    directory = _baseline_directory(root)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = _identity(directory / member)
    manifest["members"][member] = {"bytes": identity.bytes, "sha256": identity.sha256}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _write_frozen_fixture(
    root: Path,
    *,
    station_count: int = 59,
    omitted_panel_key: tuple[str, date] | None = None,
    withheld_key: tuple[str, date] = ("新營", date(2025, 5, 1)),
    extra_withheld: tuple[str, date] | None = None,
) -> tuple[CovariateReadinessConfig, list[Path]]:
    baseline = _write_baseline_fixture(
        root,
        station_count=station_count,
        omitted_panel_key=omitted_panel_key,
        withheld_key=withheld_key,
        extra_withheld=extra_withheld,
    )
    external = _write_external_inputs(root)
    return _config(), [*baseline, *external]


def test_frozen_inputs_return_sorted_baseline_tables_and_exact_identities(tmp_path: Path) -> None:
    config, expected_paths = _write_frozen_fixture(tmp_path)

    inputs = load_frozen_inputs(tmp_path, config)

    assert inputs.stations.height == 59
    assert inputs.stations["station_name"].to_list()[-1] == "新營"
    assert inputs.panel.height == 1416
    assert inputs.support.height == 59
    assert inputs.baseline_folds.height == 3 * 1416
    assert inputs.panel.filter(pl.col("target_state") == "observed").height == 1415
    assert inputs.panel.filter(pl.col("target_state") == "withheld").select(
        "station_name", "month", "mean"
    ).rows() == [("新營", date(2025, 5, 1), None)]
    assert inputs.input_files == tuple(_identity(path) for path in expected_paths)
    assert inputs.baseline_generation_sha256 == BASELINE_GENERATION
    assert inputs.station_inventory_generation_sha256 == INVENTORY_GENERATION


def test_frozen_inputs_reject_a_generation_directory_other_than_the_reviewed_one(
    tmp_path: Path,
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    directory = _baseline_directory(tmp_path)
    directory.rename(directory.with_name("c" * 64))

    with pytest.raises(RuntimeError, match="generation is missing"):
        load_frozen_inputs(tmp_path, config)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("complete", False, "complete"),
        ("generation_sha256", "c" * 64, "generation"),
    ],
)
def test_frozen_inputs_reject_an_incomplete_or_mismatched_manifest(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    manifest_path = _baseline_directory(tmp_path) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        load_frozen_inputs(tmp_path, config)


@pytest.mark.parametrize(
    "member", ["stations.parquet", "panel.parquet", "support.parquet", "folds.parquet"]
)
def test_frozen_inputs_reject_a_missing_baseline_member(tmp_path: Path, member: str) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    (_baseline_directory(tmp_path) / member).unlink()

    with pytest.raises(RuntimeError, match="missing"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_an_unexpected_baseline_table_schema(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    pl.read_parquet(panel_path).drop("target_state").write_parquet(panel_path)
    _refresh_manifest_member_identity(tmp_path, "panel.parquet")

    with pytest.raises(RuntimeError, match="panel schema"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_a_baseline_member_that_differs_from_its_manifest_identity(
    tmp_path: Path,
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path).with_columns(
        pl.when(pl.col("target_state") == "observed")
        .then(pl.col("mean") + 0.25)
        .otherwise(pl.col("mean"))
        .alias("mean")
    )
    panel.write_parquet(panel_path)

    with pytest.raises(RuntimeError, match="manifest member identity"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_duplicate_station_month_keys(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path)
    pl.concat([panel, panel.head(1)]).write_parquet(panel_path)
    _refresh_manifest_member_identity(tmp_path, "panel.parquet")

    with pytest.raises(RuntimeError, match="duplicate station-month"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_a_panel_station_missing_from_stations_or_support(
    tmp_path: Path,
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path).with_columns(
        pl.when(pl.col("station_name") == "station-01")
        .then(pl.lit("gamma"))
        .otherwise(pl.col("station_name"))
        .alias("station_name")
    )
    panel.write_parquet(panel_path)
    _refresh_manifest_member_identity(tmp_path, "panel.parquet")

    with pytest.raises(RuntimeError, match="station set"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_target_states_outside_observed_and_withheld(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path).with_columns(
        pl.when(pl.col("station_name") == "新營")
        .then(pl.lit("source_row_absent"))
        .otherwise(pl.col("target_state"))
        .alias("target_state")
    )
    panel.write_parquet(panel_path)
    _refresh_manifest_member_identity(tmp_path, "panel.parquet")

    with pytest.raises(RuntimeError, match="target states"):
        load_frozen_inputs(tmp_path, config)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("station_count", "station count"),
        ("panel_key_count", "panel key count"),
        ("withheld_count", "withheld count"),
    ],
)
def test_frozen_inputs_reject_wrong_reviewed_cohort_counts(
    tmp_path: Path, mutation: str, message: str
) -> None:
    if mutation == "station_count":
        config, _ = _write_frozen_fixture(tmp_path, station_count=58)
    elif mutation == "panel_key_count":
        config, _ = _write_frozen_fixture(
            tmp_path, omitted_panel_key=("station-00", date(2024, 1, 1))
        )
    else:
        assert mutation == "withheld_count"
        config, _ = _write_frozen_fixture(tmp_path, extra_withheld=("station-00", date(2024, 1, 1)))

    with pytest.raises(RuntimeError, match=message):
        load_frozen_inputs(tmp_path, config)


@pytest.mark.parametrize(
    "withheld_key",
    [("station-00", date(2025, 5, 1)), ("新營", date(2025, 4, 1))],
)
def test_frozen_inputs_reject_an_unreviewed_withheld_identity(
    tmp_path: Path, withheld_key: tuple[str, date]
) -> None:
    config, _ = _write_frozen_fixture(tmp_path, withheld_key=withheld_key)

    with pytest.raises(RuntimeError, match="withheld identity"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_target_state_counts_that_differ_from_folds(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    folds_path = _baseline_directory(tmp_path) / "folds.parquet"
    changed = (pl.col("target_station") == "station-00") & (pl.col("month") == date(2025, 1, 1))
    folds = pl.read_parquet(folds_path).with_columns(
        pl.when(changed)
        .then(pl.lit("withheld"))
        .otherwise(pl.col("target_state"))
        .alias("target_state"),
    )
    folds.write_parquet(folds_path)
    _refresh_manifest_member_identity(tmp_path, "folds.parquet")

    with pytest.raises(RuntimeError, match="target-state counts"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_a_station_inventory_generation_mismatch(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    manifest_path = _baseline_directory(tmp_path) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inventory_generation_sha256"] = "c" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="station inventory generation"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_a_file_changed_during_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    real_read_parquet = pl.read_parquet

    def read_then_change(path: str | Path, *args: Any, **kwargs: Any) -> pl.DataFrame:
        frame = real_read_parquet(path, *args, **kwargs)
        if Path(path) == panel_path:
            panel_path.write_bytes(panel_path.read_bytes() + b"changed")
        return frame

    monkeypatch.setattr(pl, "read_parquet", read_then_change)

    with pytest.raises(RuntimeError, match="changed while it was read"):
        load_frozen_inputs(tmp_path, config)
