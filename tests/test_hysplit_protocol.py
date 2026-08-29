"""The frozen scientific and external-path contract for HYSPLIT C0."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from twair.analysis.hysplit_protocol import (
    load_hysplit_protocol,
    validate_ascii_external_path,
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
