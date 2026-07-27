"""Tests for station identity, air-quality zone and type resolution."""

from __future__ import annotations

import polars as pl

from twair.config import load_conf
from twair.store.stations import (
    alias_map,
    derive_airzones,
    normalise_name_expr,
    station_type_map,
)

CONFIG = {
    "aliases": {"台南": "臺南", "台東": "臺東", "台西": "臺西"},
    "airzone_overrides": {"金門": "離島", "富貴角": "北部空品區"},
    "station_types": {
        "industrial": ["麥寮", "臺西"],
        "traffic": ["中壢"],
        "background": ["萬里"],
    },
    "dual_role": ["萬里"],
    "default_station_type": "general",
}


def _members(pairs: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": [p[0] for p in pairs],
            "source_member": [p[1] for p in pairs],
        }
    )


class TestNameNormalisation:
    def test_historical_spellings_map_to_the_current_name(self) -> None:
        """台南 (pre-2018 archives) and 臺南 (2023+) are the same station."""
        frame = pl.DataFrame({"station_name": ["台南", "臺南", "二林"]})

        out = frame.select(normalise_name_expr(config=CONFIG))

        assert out["station_name"].to_list() == ["臺南", "臺南", "二林"]

    def test_normalisation_merges_a_split_series(self) -> None:
        frame = pl.DataFrame({"station_name": ["台東", "臺東", "台東"]})

        out = frame.select(normalise_name_expr(config=CONFIG))

        assert out["station_name"].n_unique() == 1

    def test_unaffected_names_pass_through(self) -> None:
        frame = pl.DataFrame({"station_name": [" 沙鹿 "]})

        assert frame.select(normalise_name_expr(config=CONFIG))["station_name"][0] == "沙鹿"

    def test_alias_map_is_read_from_config(self) -> None:
        assert alias_map(CONFIG)["台南"] == "臺南"


class TestAirzoneRecovery:
    def test_zone_is_extracted_from_legacy_member_paths(self) -> None:
        """Pre-2018 archives encode the zone in the folder name; 2018+ do not."""
        frame = _members(
            [
                ("二林", "99年 中部空品區/99年二林站_20110329.csv"),
                ("萬里", "99年 北部空品區/99年萬里站_20110329.csv"),
            ]
        )

        zones = derive_airzones(frame, CONFIG)

        mapping = dict(zip(zones["station_name"], zones["airzone"], strict=True))
        assert mapping["二林"] == "中部空品區"
        assert mapping["萬里"] == "北部空品區"

    def test_zone_carries_forward_to_flat_modern_paths(self) -> None:
        frame = _members(
            [
                ("二林", "99年 中部空品區/99年二林站_20110329.csv"),
                ("二林", "二林_2025.csv"),
            ]
        )

        zones = derive_airzones(frame, CONFIG)

        assert zones.height == 1
        assert zones["airzone"].to_list() == ["中部空品區"]

    def test_override_fills_stations_that_never_appear_in_legacy_paths(self) -> None:
        frame = _members([("金門", "金門_2024.csv")])

        zones = derive_airzones(frame, CONFIG)

        assert zones["airzone"].to_list() == ["離島"]

    def test_unknown_station_gets_null_not_a_guess(self) -> None:
        """A visible gap is better than an invented zone."""
        frame = _members([("某新站", "某新站_2025.csv")])

        zones = derive_airzones(frame, CONFIG)

        assert zones["airzone"].to_list() == [None]

    def test_zone_is_resolved_after_name_normalisation(self) -> None:
        """臺西's zone lives under the old 台西 spelling in legacy paths."""
        frame = _members(
            [
                ("台西", "99年 雲嘉南空品區/99年台西站_20110329.csv"),
                ("臺西", "臺西_2025.csv"),
            ]
        )

        zones = derive_airzones(frame, CONFIG)

        assert zones.height == 1
        assert zones["station_name"].to_list() == ["臺西"]
        assert zones["airzone"].to_list() == ["雲嘉南空品區"]


class TestStationTypes:
    def test_types_are_inverted_from_the_config_lists(self) -> None:
        types = station_type_map(CONFIG)

        assert types["麥寮"] == "industrial"
        assert types["中壢"] == "traffic"

    def test_unlisted_stations_are_not_in_the_map(self) -> None:
        assert "二林" not in station_type_map(CONFIG)


class TestShippedConfig:
    def test_station_types_match_the_1994_readme_counts(self) -> None:
        """ReadMe: 工業 5, 交通 6, 國家公園 2, 背景 4, 參考 2."""
        conf = load_conf("stations")["station_types"]

        assert len(conf["industrial"]) == 5
        assert len(conf["traffic"]) == 6
        assert len(conf["national_park"]) == 2
        assert len(conf["background"]) == 4
        assert len(conf["reference"]) == 2

    def test_industrial_list_uses_normalised_names(self) -> None:
        """臺西 is industrial; the config must not use the retired 台西 spelling."""
        conf = load_conf("stations")

        assert "臺西" in conf["station_types"]["industrial"]
        assert "台西" not in conf["station_types"]["industrial"]

    def test_dual_role_stations_are_also_typed(self) -> None:
        conf = load_conf("stations")
        typed = station_type_map(conf)

        for name in conf["dual_role"]:
            assert name in typed, f"{name} is dual-role but has no primary type"
