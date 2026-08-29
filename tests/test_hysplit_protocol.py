"""The frozen scientific and external-path contract for HYSPLIT C0."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twair.analysis.hysplit_protocol import (
    PilotPlan,
    build_hysplit_pilot_plan,
    load_hysplit_pilot_plan,
    load_hysplit_protocol,
    validate_ascii_external_path,
    write_hysplit_pilot_plan,
)
from twair.config import ConfigError


def _valid_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "analysis": {
            "year": 2025,
            "pollutant": "PM2.5",
            "receptors": ["富貴角", "麥寮", "楠梓", "花蓮"],
            "event_percentile": 90.0,
            "control_percentile": 50.0,
            "max_events_per_station": 30,
            "event_separation_hours": 72,
            "calm_threshold_ms": 0.5,
            "direction_sector_degrees": 30,
            "speed_bins_ms": [0.5, 1.5, 2.5, 4.0, 6.0, 8.0],
            "matching": {
                "same_month": True,
                "same_local_hour": True,
                "same_direction_sector": True,
                "same_speed_bin": True,
                "without_replacement": True,
                "allow_relaxation": False,
            },
            "duration_hours": -72,
            "start_heights_m_agl": [100, 300, 500],
            "vertical_motion": 0,
            "model_top_m_agl": 10000.0,
            "meteorology_dataset": "gdas1",
            "official_sources": {
                "download": "https://www.ready.noaa.gov/HYSPLIT_hytrial.php",
                "control_format": "https://www.ready.noaa.gov/hysplitusersguide/S262.htm",
                "endpoint_format": "https://www.ready.noaa.gov/hysplitusersguide/S263.htm",
                "gdas1": "https://www.ready.noaa.gov/gdas1.php",
            },
            "claim_boundary": {
                "pathway_only": True,
                "source_identity": False,
                "source_location": False,
                "source_contribution": False,
                "causal_attribution": False,
                "concentration_field": False,
            },
        },
    }


def test_shipped_protocol_drives_the_approved_c0_run_contract() -> None:
    protocol = load_hysplit_protocol()

    assert protocol.year == 2025
    assert protocol.receptors == ("富貴角", "麥寮", "楠梓", "花蓮")
    assert protocol.event_percentile == 90.0
    assert protocol.control_percentile == 50.0
    assert protocol.max_events_per_station == 30
    assert protocol.event_separation_hours == 72
    assert protocol.calm_threshold_ms == 0.5
    assert protocol.direction_sector_degrees == 30
    assert protocol.start_heights_m_agl == (100, 300, 500)
    assert protocol.duration_hours == -72
    assert protocol.vertical_motion == 0


def test_protocol_rejects_matching_relaxation() -> None:
    config = _valid_config()
    config["analysis"]["matching"]["allow_relaxation"] = True

    with pytest.raises(ConfigError, match="matching relaxation is forbidden"):
        load_hysplit_protocol(config)


@pytest.mark.parametrize(
    ("field", "changed", "message"),
    [
        ("receptors", ["富貴角", "麥寮", "楠梓"], "receptors"),
        ("speed_bins_ms", [0.5, 2.5, 8.0], "speed bins"),
        ("duration_hours", 72, "duration"),
        ("vertical_motion", 5, "vertical motion"),
    ],
)
def test_protocol_rejects_changes_that_would_create_a_different_experiment(
    field: str,
    changed: object,
    message: str,
) -> None:
    config = _valid_config()
    config["analysis"][field] = changed

    with pytest.raises(ConfigError, match=message):
        load_hysplit_protocol(config)


def test_protocol_rejects_a_claim_boundary_that_allows_source_identity() -> None:
    config = deepcopy(_valid_config())
    config["analysis"]["claim_boundary"]["source_identity"] = True

    with pytest.raises(ConfigError, match="claim boundary"):
        load_hysplit_protocol(config)


@pytest.mark.parametrize("leaf", ["work with space", "軌跡"])
def test_external_paths_reject_whitespace_or_non_ascii(tmp_path: Path, leaf: str) -> None:
    with pytest.raises(ValueError, match="ASCII-only"):
        validate_ascii_external_path(tmp_path / leaf, label="HYSPLIT work directory")


def test_external_paths_reject_relative_paths() -> None:
    with pytest.raises(ValueError, match="absolute"):
        validate_ascii_external_path(Path("relative"), label="HYSPLIT work directory")


def test_external_paths_accept_an_absolute_ascii_path(tmp_path: Path) -> None:
    path = tmp_path / "hysplit"

    assert validate_ascii_external_path(path, label="HYSPLIT work directory") == path.absolute()


def _synthetic_geography() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": ["富貴角", "麥寮", "楠梓", "花蓮"],
            "station_name_en": ["Fugueijiao", "Mailiao", "Nanzi", "Hualien"],
            "lon": [121.536, 120.252, 120.328, 121.599],
            "lat": [25.298, 23.754, 22.733, 23.971],
        }
    )


def _synthetic_wind_frame() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    start = datetime(2025, 1, 1, 1, 30)
    for day in range(27):
        rows.append(
            {
                "station_name": "富貴角",
                "ts_local": start + timedelta(days=day),
                "PM2.5": 10.0,
                "WS_HR": 3.0,
                "WD_HR": 200.0,
            }
        )

    # Equidistant eligible controls: the earlier timestamp must win.
    rows[7].update(
        ts_local=datetime(2025, 1, 8, 12, 30),
        WS_HR=2.0,
        WD_HR=10.0,
    )
    rows[11].update(
        ts_local=datetime(2025, 1, 12, 12, 30),
        WS_HR=2.0,
        WD_HR=10.0,
    )
    rows.extend(
        [
            {
                "station_name": "富貴角",
                "ts_local": datetime(2025, 1, 10, 12, 30),
                "PM2.5": 100.0,
                "WS_HR": 2.0,
                "WD_HR": 10.0,
            },
            {
                "station_name": "富貴角",
                "ts_local": datetime(2025, 1, 14, 12, 45),
                "PM2.5": 90.0,
                "WS_HR": 5.0,
                "WD_HR": 100.0,
            },
            # High but within 72 hours of the top-ranked retained event.
            {
                "station_name": "富貴角",
                "ts_local": datetime(2025, 1, 11, 12, 15),
                "PM2.5": 80.0,
                "WS_HR": 2.0,
                "WD_HR": 10.0,
            },
            # A calm extreme must never become an event.
            {
                "station_name": "富貴角",
                "ts_local": datetime(2025, 1, 25, 12, 30),
                "PM2.5": 1000.0,
                "WS_HR": 0.5,
                "WD_HR": 10.0,
            },
        ]
    )
    return pl.DataFrame(rows)


def test_pilot_plan_selects_and_matches_without_relaxation_or_randomness() -> None:
    plan = build_hysplit_pilot_plan(
        _synthetic_wind_frame(),
        _synthetic_geography(),
        load_hysplit_protocol(),
    )
    events = plan.arrivals.filter(pl.col("arrival_kind") == "event")
    controls = plan.arrivals.filter(pl.col("arrival_kind") == "control")

    assert events["event_rank"].to_list() == [1, 2]
    assert events["match_state"].to_list() == ["matched", "unmatched_no_control"]
    assert controls["ts_local"].to_list() == [datetime(2025, 1, 8, 12, 30)]
    assert controls["ts_local"].n_unique() == controls.height
    assert plan.runs.height == controls.height * 2 * 3
    assert plan.runs["duration_hours"].unique().to_list() == [-72]
    assert plan.runs["start_height_m_agl"].unique().sort().to_list() == [100, 300, 500]

    expected_utc = [
        value.replace(tzinfo=UTC) - timedelta(hours=8) for value in events["ts_local"].to_list()
    ]
    assert events["arrival_utc"].to_list() == expected_utc
    assert events["arrival_utc"].dt.minute().to_list() == [30, 45]
    assert plan.summary == {
        "selected_events": 2,
        "matched_pairs": 1,
        "unmatched_events": 1,
        "standard_runs": 6,
    }


def test_pilot_plan_rejects_incomplete_or_duplicate_receptor_geography() -> None:
    frame = _synthetic_wind_frame()
    geography = _synthetic_geography()

    with pytest.raises(ConfigError, match="one geography row per receptor"):
        build_hysplit_pilot_plan(frame, geography.head(3), load_hysplit_protocol())
    with pytest.raises(ConfigError, match="one geography row per receptor"):
        build_hysplit_pilot_plan(
            frame,
            pl.concat([geography, geography.head(1)]),
            load_hysplit_protocol(),
        )


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("lon", float("nan"), "finite coordinates"),
        ("lat", 30.0, "outside Taiwan"),
        ("station_name_en", "", "English name"),
    ],
)
def test_pilot_plan_rejects_unusable_receptor_geography(
    column: str,
    value: object,
    message: str,
) -> None:
    geography = _synthetic_geography().with_columns(
        pl.when(pl.col("station_name") == "富貴角")
        .then(pl.lit(value))
        .otherwise(pl.col(column))
        .alias(column)
    )

    with pytest.raises(ConfigError, match=message):
        build_hysplit_pilot_plan(
            _synthetic_wind_frame(),
            geography,
            load_hysplit_protocol(),
        )


def _synthetic_plan() -> PilotPlan:
    return build_hysplit_pilot_plan(
        _synthetic_wind_frame(),
        _synthetic_geography(),
        load_hysplit_protocol(),
    )


def test_immutable_plan_write_is_content_addressed_and_independently_reloadable(
    tmp_path: Path,
) -> None:
    plan = _synthetic_plan()

    first = write_hysplit_pilot_plan(plan, output_root=tmp_path / "output")
    second = write_hysplit_pilot_plan(plan, output_root=tmp_path / "output")
    loaded = load_hysplit_pilot_plan(first["manifest"].parent)

    assert first == second
    assert first["manifest"].parent.name == first["manifest"].parent.name.lower()
    assert len(first["manifest"].parent.name) == 64
    assert loaded.arrivals.equals(plan.arrivals)
    assert loaded.runs.equals(plan.runs)
    assert loaded.summary == plan.summary
    assert loaded.input_identities == plan.input_identities


def test_changed_arrival_produces_a_different_generation(tmp_path: Path) -> None:
    plan = _synthetic_plan()
    changed = replace(
        plan,
        arrivals=plan.arrivals.with_columns((pl.col("pm25") + 0.25).alias("pm25")),
    )

    original = write_hysplit_pilot_plan(plan, output_root=tmp_path / "output")
    modified = write_hysplit_pilot_plan(changed, output_root=tmp_path / "output")

    assert original["manifest"].parent != modified["manifest"].parent


@pytest.mark.parametrize("damage", ["arrivals", "summary", "extra"])
def test_plan_loader_rejects_tampering_or_unexpected_members(
    tmp_path: Path,
    damage: str,
) -> None:
    written = write_hysplit_pilot_plan(_synthetic_plan(), output_root=tmp_path / "output")
    directory = written["manifest"].parent
    if damage == "arrivals":
        with (directory / "arrivals.parquet").open("ab") as stream:
            stream.write(b"changed")
    elif damage == "summary":
        (directory / "summary.json").write_text("{}", encoding="utf-8")
    else:
        (directory / "unexpected.txt").write_text("unexpected", encoding="ascii")

    with pytest.raises(RuntimeError):
        load_hysplit_pilot_plan(directory)
