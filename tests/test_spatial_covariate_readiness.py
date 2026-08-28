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


def _write_baseline_fixture(root: Path) -> list[Path]:
    directory = _baseline_directory(root)
    directory.mkdir(parents=True)
    stations = pl.DataFrame(
        {
            "station_name": ["alpha", "beta"],
            "station_type_official": ["一般站", "背景站"],
            "lon": [121.0, 121.1],
            "lat": [24.0, 24.1],
        },
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["stations"],
    )
    panel = pl.DataFrame(
        {
            "station_name": ["alpha", "alpha", "beta", "beta"],
            "station_type_official": ["一般站", "一般站", "背景站", "背景站"],
            "lon": [121.0, 121.0, 121.1, 121.1],
            "lat": [24.0, 24.0, 24.1, 24.1],
            "month": [date(2024, 1, 1), date(2025, 1, 1), date(2024, 1, 1), date(2025, 1, 1)],
            "pollutant": ["PM2.5"] * 4,
            "mean": [10.0, 11.0, 12.0, None],
            "meets_threshold": [True, True, True, False],
            "target_state": ["observed", "observed", "observed", "withheld"],
        },
        schema=SPATIAL_BASELINE_TABLE_SCHEMAS["panel"],
    )
    support = pl.DataFrame(
        {
            "station_name": ["alpha", "beta"],
            "nearest_station": ["beta", "alpha"],
            "nearest_station_km": [15.0, 15.0],
            "stations_within_20km": [1, 1],
            "stations_within_40km": [1, 1],
            "x_m": [100.0, 200.0],
            "y_m": [300.0, 400.0],
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
                    "target_cluster": 0 if station_name == "alpha" else 1,
                    "target_state": target_state,
                    "observed": row["mean"],
                    "train_stations": ["beta"] if station_name == "alpha" else ["alpha"],
                    "n_train": 1,
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
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "complete": True,
                "generation_sha256": BASELINE_GENERATION,
                "inventory_generation_sha256": INVENTORY_GENERATION,
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


def _write_frozen_fixture(root: Path) -> tuple[CovariateReadinessConfig, list[Path]]:
    baseline = _write_baseline_fixture(root)
    external = _write_external_inputs(root)
    return _config(), [*baseline, *external]


def test_frozen_inputs_return_sorted_baseline_tables_and_exact_identities(tmp_path: Path) -> None:
    config, expected_paths = _write_frozen_fixture(tmp_path)

    inputs = load_frozen_inputs(tmp_path, config)

    assert inputs.stations["station_name"].to_list() == ["alpha", "beta"]
    assert inputs.panel.select("station_name", "month").rows() == [
        ("alpha", date(2024, 1, 1)),
        ("alpha", date(2025, 1, 1)),
        ("beta", date(2024, 1, 1)),
        ("beta", date(2025, 1, 1)),
    ]
    assert inputs.support["station_name"].to_list() == ["alpha", "beta"]
    assert (
        inputs.baseline_folds.filter(pl.col("evaluation") == "buffer_20km")
        .select("target_station", "month")
        .rows()
    ) == [
        ("alpha", date(2024, 1, 1)),
        ("beta", date(2024, 1, 1)),
        ("alpha", date(2025, 1, 1)),
        ("beta", date(2025, 1, 1)),
    ]
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

    with pytest.raises(RuntimeError, match="panel schema"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_duplicate_station_month_keys(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path)
    pl.concat([panel, panel.head(1)]).write_parquet(panel_path)

    with pytest.raises(RuntimeError, match="duplicate station-month"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_a_panel_station_missing_from_stations_or_support(
    tmp_path: Path,
) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path).with_columns(
        pl.when(pl.col("station_name") == "beta")
        .then(pl.lit("gamma"))
        .otherwise(pl.col("station_name"))
        .alias("station_name")
    )
    panel.write_parquet(panel_path)

    with pytest.raises(RuntimeError, match="station set"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_target_states_outside_observed_and_withheld(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    panel = pl.read_parquet(panel_path).with_columns(
        pl.when(pl.col("station_name") == "alpha")
        .then(pl.lit("source_row_absent"))
        .otherwise(pl.col("target_state"))
        .alias("target_state")
    )
    panel.write_parquet(panel_path)

    with pytest.raises(RuntimeError, match="target states"):
        load_frozen_inputs(tmp_path, config)


def test_frozen_inputs_reject_target_state_counts_that_differ_from_folds(tmp_path: Path) -> None:
    config, _ = _write_frozen_fixture(tmp_path)
    panel_path = _baseline_directory(tmp_path) / "panel.parquet"
    changed = (pl.col("station_name") == "alpha") & (pl.col("month") == date(2025, 1, 1))
    panel = pl.read_parquet(panel_path).with_columns(
        pl.when(changed)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("mean"))
        .alias("mean"),
        pl.when(changed)
        .then(pl.lit(False))
        .otherwise(pl.col("meets_threshold"))
        .alias("meets_threshold"),
        pl.when(changed)
        .then(pl.lit("withheld"))
        .otherwise(pl.col("target_state"))
        .alias("target_state"),
    )
    panel.write_parquet(panel_path)

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
