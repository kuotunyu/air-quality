"""The Hugging Face package is a second delivery surface, not a second dataset.

These tests keep it downstream of the published L0/L1 aggregates and make the
raw-hourly redistribution boundary executable.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest
import yaml


def _write_public_layers(root: Path) -> None:
    (root / "l0").mkdir(parents=True)
    (root / "l1").mkdir(parents=True)
    (root / "l0" / "pm25.json").write_text(
        json.dumps(
            {
                "pollutant": "PM2.5",
                "name_zh": "細懸浮微粒",
                "unit": "ug/m3",
                "precision": 2,
                "months": ["2025-01", "2025-02"],
                "stations": ["甲站", "乙站"],
                "mean": [[12.5, None], [None, None]],
                "n_days": [[31, 1], [0, 0]],
                "null_means": {
                    "n_days == 0": "station not reporting this measurand that month",
                    "n_days > 0": "aggregate withheld: coverage below threshold",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    pl.DataFrame(
        {
            "station_name": ["甲站", "甲站", "乙站"],
            "date": [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 1)],
            "mean": [12.5, None, None],
            "n_valid": [24, 3, 0],
        },
        schema_overrides={"mean": pl.Float32, "n_valid": pl.UInt8},
    ).write_parquet(root / "l1" / "pm25.parquet")


def test_public_layers_become_two_loadable_parquet_configurations(tmp_path: Path) -> None:
    from twair.viz.hf_dataset import build_dataset_bundle

    source = tmp_path / "public"
    destination = tmp_path / "dataset"
    _write_public_layers(source)

    report = build_dataset_bundle(source=source, destination=destination)

    monthly = pl.read_parquet(destination / "data" / "monthly" / "pm25.parquet")
    daily = pl.read_parquet(destination / "data" / "daily" / "pm25.parquet")
    assert monthly.schema == {
        "pollutant": pl.String,
        "name_zh": pl.String,
        "unit": pl.String,
        "station_name": pl.String,
        "month": pl.Date,
        "mean": pl.Float32,
        "n_days": pl.UInt8,
    }
    assert daily.schema == {
        "pollutant": pl.String,
        "name_zh": pl.String,
        "unit": pl.String,
        "station_name": pl.String,
        "date": pl.Date,
        "mean": pl.Float32,
        "n_valid": pl.UInt8,
    }
    assert monthly.height == 4
    assert daily.height == 3
    assert report.rows == {"monthly": 4, "daily": 3}

    card = (destination / "README.md").read_text(encoding="utf-8")
    front_matter = yaml.safe_load(card.split("---", 2)[1])
    assert [config["config_name"] for config in front_matter["configs"]] == [
        "monthly",
        "daily",
    ]
    assert front_matter["configs"][0]["default"] is True
    assert {item["data_files"][0]["split"] for item in front_matter["configs"]} == {"full"}


def test_both_kinds_of_null_survive_the_monthly_and_daily_packages(tmp_path: Path) -> None:
    from twair.viz.hf_dataset import build_dataset_bundle

    source = tmp_path / "public"
    destination = tmp_path / "dataset"
    _write_public_layers(source)

    build_dataset_bundle(source=source, destination=destination)

    monthly = pl.read_parquet(destination / "data" / "monthly" / "pm25.parquet")
    daily = pl.read_parquet(destination / "data" / "daily" / "pm25.parquet")
    assert monthly.filter(pl.col("mean").is_null() & (pl.col("n_days") == 0)).height == 2
    assert monthly.filter(pl.col("mean").is_null() & (pl.col("n_days") > 0)).height == 1
    assert daily.filter(pl.col("mean").is_null() & (pl.col("n_valid") == 0)).height == 1
    assert daily.filter(pl.col("mean").is_null() & (pl.col("n_valid") > 0)).height == 1


def test_the_manifest_measures_every_data_file_without_listing_l2(tmp_path: Path) -> None:
    from twair.viz.hf_dataset import build_dataset_bundle

    source = tmp_path / "public"
    destination = tmp_path / "dataset"
    _write_public_layers(source)

    build_dataset_bundle(source=source, destination=destination)

    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["levels"] == ["L0", "L1"]
    assert {item["path"] for item in manifest["files"]} == {
        "data/monthly/pm25.parquet",
        "data/daily/pm25.parquet",
    }
    assert all(item["rows"] > 0 for item in manifest["files"])
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])
    assert not any("l2" in path.as_posix().lower() for path in destination.rglob("*"))


def test_a_raw_hourly_source_is_rejected_instead_of_being_packaged(tmp_path: Path) -> None:
    from twair.viz.hf_dataset import build_dataset_bundle

    source = tmp_path / "public"
    _write_public_layers(source)
    (source / "l2").mkdir()

    with pytest.raises(RuntimeError, match="L2"):
        build_dataset_bundle(source=source, destination=tmp_path / "dataset")


def test_overwrite_never_replaces_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twair.viz import hf_dataset

    repo_root = tmp_path / "repo"
    elsewhere = tmp_path / "elsewhere"
    repo_root.mkdir()
    elsewhere.mkdir()
    sentinel = repo_root / "keep.txt"
    sentinel.write_text("must survive\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(hf_dataset, "REPO_ROOT", repo_root, raising=False)

    with pytest.raises(RuntimeError, match="broad directory"):
        hf_dataset._prepare_destination(repo_root, overwrite=True)

    assert sentinel.exists()


def test_overwrite_never_replaces_a_parent_of_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twair.viz import hf_dataset

    parent = tmp_path / "parent"
    repo_root = parent / "repo"
    elsewhere = tmp_path / "elsewhere"
    repo_root.mkdir(parents=True)
    elsewhere.mkdir()
    sentinel = repo_root / "keep.txt"
    sentinel.write_text("must survive\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(hf_dataset, "REPO_ROOT", repo_root, raising=False)

    with pytest.raises(RuntimeError, match="broad directory"):
        hf_dataset._prepare_destination(parent, overwrite=True)

    assert sentinel.exists()


def test_missing_daily_layer_fails_before_the_destination_is_created(tmp_path: Path) -> None:
    from twair.viz.hf_dataset import build_dataset_bundle

    source = tmp_path / "public"
    destination = tmp_path / "dataset"
    _write_public_layers(source)
    (source / "l1" / "pm25.parquet").unlink()

    with pytest.raises(FileNotFoundError, match="complete public export"):
        build_dataset_bundle(source=source, destination=destination)

    assert not destination.exists()
