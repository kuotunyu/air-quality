from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twair.analysis.spatial_surface_baseline import (
    SpatialSurfaceBaselineConfig,
    load_spatial_surface_baseline_config,
    load_surface_inputs,
)
from twair.config import ConfigError


def _config(*, spatial_folds: int = 3) -> SpatialSurfaceBaselineConfig:
    return replace(
        load_spatial_surface_baseline_config(),
        spatial_folds=spatial_folds,
        bootstrap_draws=99,
    )


def write_surface_fixture(root: Path) -> Path:
    """Write qc/stations.parquet and monthly/monthly.parquet for 2024–2025.

    The inventory has general stations 一般甲/一般乙, background station 背景,
    industrial station 工業, and offshore general station 馬公. The accepted
    three stations each have 24 monthly PM2.5 rows; 背景/2025-12 has a present
    row with mean=null and meets_threshold=false.
    """
    stations_path = root / "outputs" / "qc" / "stations.parquet"
    monthly_path = root / "processed" / "monthly" / "monthly.parquet"
    stations_path.parent.mkdir(parents=True)
    monthly_path.parent.mkdir(parents=True)

    pl.DataFrame(
        {
            "station_name": ["背景", "一般甲", "一般乙", "工業", "馬公"],
            "station_type_official": ["背景站", "一般站", "一般站", "工業站", "一般站"],
            "lon": [121.3, 121.1, 121.2, 121.4, 119.6],
            "lat": [24.3, 24.1, 24.2, 24.4, 23.6],
        },
        schema={
            "station_name": pl.String,
            "station_type_official": pl.String,
            "lon": pl.Float64,
            "lat": pl.Float64,
        },
    ).write_parquet(stations_path)

    rows: list[dict[str, object]] = []
    for station_index, station in enumerate(["一般甲", "一般乙", "背景", "工業", "馬公"]):
        for year in (2024, 2025):
            for month in range(1, 13):
                withheld = station == "背景" and year == 2025 and month == 12
                rows.append(
                    {
                        "station_name": station,
                        "pollutant": "PM2.5",
                        "month": date(year, month, 1),
                        "mean": None if withheld else float(10 + station_index + month),
                        "meets_threshold": not withheld,
                    }
                )
    rows.append(
        {
            "station_name": "一般甲",
            "pollutant": "PM10",
            "month": date(2024, 1, 1),
            "mean": 999.0,
            "meets_threshold": True,
        }
    )
    pl.DataFrame(
        rows,
        schema={
            "station_name": pl.String,
            "pollutant": pl.String,
            "month": pl.Date,
            "mean": pl.Float64,
            "meets_threshold": pl.Boolean,
        },
    ).write_parquet(monthly_path)
    return root


def _stations_path(root: Path) -> Path:
    return root / "outputs" / "qc" / "stations.parquet"


def _monthly_path(root: Path) -> Path:
    return root / "processed" / "monthly" / "monthly.parquet"


def test_shipped_config_pins_the_reviewed_baseline_contract() -> None:
    config = load_spatial_surface_baseline_config()

    assert config.years == (2024, 2025)
    assert config.pollutant == "PM2.5"
    assert config.station_types == ("一般站", "背景站")
    assert config.excluded_stations == ("馬公", "金門", "馬祖")
    assert config.buffer_radii_km == (20.0, 40.0)
    assert config.methods == (
        "station_mean",
        "nearest",
        "idw2",
        "kriging_spherical",
        "kriging_hole_effect",
    )
    assert config.min_train_stations == 8
    assert config.seed == 20260828


def test_config_rejects_missing_or_non_mapping_blocks() -> None:
    invalids: tuple[dict[str, object], ...] = ({}, {"analysis": []})
    for invalid in invalids:
        with pytest.raises(ConfigError):
            load_spatial_surface_baseline_config(invalid)
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_spatial_surface_baseline_config({"schema_version": 1, "analysis": []})


def test_primary_cohort_keeps_the_complete_key_grid_and_withheld_mean(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    inputs = load_surface_inputs(root, _config(spatial_folds=2))

    assert inputs.stations["station_name"].to_list() == ["背景", "一般甲", "一般乙"]
    assert inputs.panel.height == 3 * 24
    assert inputs.panel.filter(pl.col("target_state") == "withheld").height == 1
    assert inputs.panel.filter(pl.col("target_state") == "observed").height == 71
    assert inputs.panel.filter(pl.col("mean").is_null()).height == 1


def test_duplicate_station_names_are_rejected(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    stations = pl.read_parquet(_stations_path(root))
    pl.concat([stations, stations.head(1)]).write_parquet(_stations_path(root))

    with pytest.raises(RuntimeError, match="not unique"):
        load_surface_inputs(root, _config())


def test_partial_coordinates_are_rejected(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    stations = pl.read_parquet(_stations_path(root)).with_columns(
        pl.when(pl.col("station_name") == "一般甲")
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("lon"))
        .alias("lon")
    )
    stations.write_parquet(_stations_path(root))

    with pytest.raises(RuntimeError, match="both present or both null"):
        load_surface_inputs(root, _config())


@pytest.mark.parametrize("invalid_coordinate", [float("nan"), 150.0])
def test_non_finite_or_outside_taiwan_coordinates_are_rejected(
    tmp_path: Path, invalid_coordinate: float
) -> None:
    root = write_surface_fixture(tmp_path)
    stations = pl.read_parquet(_stations_path(root)).with_columns(
        pl.when(pl.col("station_name") == "一般甲")
        .then(pl.lit(invalid_coordinate))
        .otherwise(pl.col("lon"))
        .alias("lon")
    )
    stations.write_parquet(_stations_path(root))

    with pytest.raises(RuntimeError, match=r"not finite|outside Taiwan"):
        load_surface_inputs(root, _config())


def test_duplicate_station_month_keys_are_rejected(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    monthly = pl.read_parquet(_monthly_path(root))
    duplicate = monthly.filter(
        (pl.col("station_name") == "一般甲") & (pl.col("pollutant") == "PM2.5")
    ).head(1)
    pl.concat([monthly, duplicate]).write_parquet(_monthly_path(root))

    with pytest.raises(RuntimeError, match=r"duplicate PM2.5 station-month"):
        load_surface_inputs(root, _config())


def test_a_missing_source_calendar_key_remains_explicitly_absent(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    monthly = pl.read_parquet(_monthly_path(root)).filter(
        ~((pl.col("station_name") == "一般乙") & (pl.col("month") == date(2024, 6, 1)))
    )
    monthly.write_parquet(_monthly_path(root))

    inputs = load_surface_inputs(root, _config())
    missing = inputs.panel.filter(
        (pl.col("station_name") == "一般乙") & (pl.col("month") == date(2024, 6, 1))
    )
    assert missing.height == 1
    assert missing.item(0, "target_state") == "source_row_absent"


def test_non_pm25_rows_cannot_enter_the_panel(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    inputs = load_surface_inputs(root, _config())

    assert inputs.panel["pollutant"].unique().to_list() == ["PM2.5"]
    assert inputs.panel.filter(pl.col("mean") == 999.0).is_empty()


def test_source_file_changes_while_read_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_surface_fixture(tmp_path)
    monthly_path = _monthly_path(root)
    real_read_parquet = pl.read_parquet

    def read_then_change(path: str | Path, *args: Any, **kwargs: Any) -> pl.DataFrame:
        frame = real_read_parquet(path, *args, **kwargs)
        if Path(path) == monthly_path:
            monthly_path.write_bytes(monthly_path.read_bytes() + b"changed")
        return frame

    monkeypatch.setattr(pl, "read_parquet", read_then_change)

    with pytest.raises(RuntimeError, match="changed while it was read"):
        load_surface_inputs(root, _config())


def test_invalid_non_finite_monthly_means_are_not_observed(tmp_path: Path) -> None:
    root = write_surface_fixture(tmp_path)
    monthly = pl.read_parquet(_monthly_path(root)).with_columns(
        pl.when((pl.col("station_name") == "一般甲") & (pl.col("month") == date(2024, 2, 1)))
        .then(pl.lit(math.inf))
        .otherwise(pl.col("mean"))
        .alias("mean")
    )
    monthly.write_parquet(_monthly_path(root))

    inputs = load_surface_inputs(root, _config())
    state = inputs.panel.filter(
        (pl.col("station_name") == "一般甲") & (pl.col("month") == date(2024, 2, 1))
    ).item(0, "target_state")
    assert state == "invalid_non_finite"
