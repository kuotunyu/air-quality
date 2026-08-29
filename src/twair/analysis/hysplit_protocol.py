"""Frozen scientific contract for the four-receptor HYSPLIT pilot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from twair.config import ConfigError, load_conf

_RECEPTORS = ("富貴角", "麥寮", "楠梓", "花蓮")
_SPEED_BINS_MS = (0.5, 1.5, 2.5, 4.0, 6.0, 8.0)
_START_HEIGHTS_M_AGL = (100, 300, 500)
_MATCHING = {
    "same_month": True,
    "same_local_hour": True,
    "same_direction_sector": True,
    "same_speed_bin": True,
    "without_replacement": True,
    "allow_relaxation": False,
}
_OFFICIAL_SOURCES = {
    "download": "https://www.ready.noaa.gov/HYSPLIT_hytrial.php",
    "control_format": "https://www.ready.noaa.gov/hysplitusersguide/S262.htm",
    "endpoint_format": "https://www.ready.noaa.gov/hysplitusersguide/S263.htm",
    "gdas1": "https://www.ready.noaa.gov/gdas1.php",
}
_CLAIM_BOUNDARY = {
    "pathway_only": True,
    "source_identity": False,
    "source_location": False,
    "source_contribution": False,
    "causal_attribution": False,
    "concentration_field": False,
}
_EXPECTED_ANALYSIS: dict[str, object] = {
    "year": 2025,
    "pollutant": "PM2.5",
    "receptors": list(_RECEPTORS),
    "event_percentile": 90.0,
    "control_percentile": 50.0,
    "max_events_per_station": 30,
    "event_separation_hours": 72,
    "calm_threshold_ms": 0.5,
    "direction_sector_degrees": 30,
    "speed_bins_ms": list(_SPEED_BINS_MS),
    "matching": _MATCHING,
    "duration_hours": -72,
    "start_heights_m_agl": list(_START_HEIGHTS_M_AGL),
    "vertical_motion": 0,
    "model_top_m_agl": 10000.0,
    "meteorology_dataset": "gdas1",
    "official_sources": _OFFICIAL_SOURCES,
    "claim_boundary": _CLAIM_BOUNDARY,
}
_FIELD_LABELS = {
    "receptors": "receptors",
    "speed_bins_ms": "speed bins",
    "duration_hours": "duration",
    "vertical_motion": "vertical motion",
    "claim_boundary": "claim boundary",
}


@dataclass(frozen=True, slots=True)
class HysplitProtocol:
    """Reviewed constants that define the pilot and its public claim boundary."""

    year: int
    pollutant: str
    receptors: tuple[str, ...]
    event_percentile: float
    control_percentile: float
    max_events_per_station: int
    event_separation_hours: int
    calm_threshold_ms: float
    direction_sector_degrees: int
    speed_bins_ms: tuple[float, ...]
    matching: Mapping[str, bool]
    duration_hours: int
    start_heights_m_agl: tuple[int, ...]
    vertical_motion: int
    model_top_m_agl: float
    meteorology_dataset: str
    official_sources: Mapping[str, str]
    claim_boundary: Mapping[str, bool]


def _same_typed_value(actual: object, expected: object) -> bool:
    """Compare nested config values without treating ``True`` as integer ``1``."""
    if isinstance(expected, dict):
        if not isinstance(actual, Mapping) or set(actual) != set(expected):
            return False
        return all(
            _same_typed_value(actual[key], expected_value)
            for key, expected_value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(
            _same_typed_value(actual_value, expected_value)
            for actual_value, expected_value in zip(actual, expected, strict=True)
        )
    return type(actual) is type(expected) and actual == expected


def load_hysplit_protocol(
    config: Mapping[str, object] | None = None,
) -> HysplitProtocol:
    """Load the reviewed protocol, rejecting any silent experimental drift."""
    raw: Mapping[str, object] = load_conf("hysplit_pilot") if config is None else config
    if not _same_typed_value(raw.get("schema_version"), 1):
        raise ConfigError("HYSPLIT protocol schema version must be exactly 1")

    analysis = raw.get("analysis")
    if not isinstance(analysis, Mapping):
        raise ConfigError("HYSPLIT protocol analysis must be a mapping")

    matching = analysis.get("matching")
    if isinstance(matching, Mapping) and matching.get("allow_relaxation") is True:
        raise ConfigError("HYSPLIT matching relaxation is forbidden")

    if set(analysis) != set(_EXPECTED_ANALYSIS):
        raise ConfigError("HYSPLIT analysis fields differ from the reviewed protocol")
    for field, expected in _EXPECTED_ANALYSIS.items():
        if not _same_typed_value(analysis.get(field), expected):
            label = _FIELD_LABELS.get(field, field.replace("_", " "))
            raise ConfigError(f"HYSPLIT {label} differs from the reviewed protocol")

    return HysplitProtocol(
        year=2025,
        pollutant="PM2.5",
        receptors=_RECEPTORS,
        event_percentile=90.0,
        control_percentile=50.0,
        max_events_per_station=30,
        event_separation_hours=72,
        calm_threshold_ms=0.5,
        direction_sector_degrees=30,
        speed_bins_ms=_SPEED_BINS_MS,
        matching=MappingProxyType(dict(_MATCHING)),
        duration_hours=-72,
        start_heights_m_agl=_START_HEIGHTS_M_AGL,
        vertical_motion=0,
        model_top_m_agl=10000.0,
        meteorology_dataset="gdas1",
        official_sources=MappingProxyType(dict(_OFFICIAL_SOURCES)),
        claim_boundary=MappingProxyType(dict(_CLAIM_BOUNDARY)),
    )


def validate_ascii_external_path(path: Path, *, label: str) -> Path:
    """Validate a future external-tool path without touching the filesystem."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError(f"{label} must be absolute")
    rendered = str(expanded)
    if any(ord(character) >= 128 or character.isspace() for character in rendered):
        raise ValueError(f"{label} must be ASCII-only and contain no whitespace")
    return expanded.absolute()
