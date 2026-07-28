"""Tests for station identity, air-quality zone and type resolution."""

from __future__ import annotations

import polars as pl
import pytest

from twair.config import load_conf  # type: ignore[import-untyped]
from twair.store.stations import (  # type: ignore[import-untyped]
    alias_map,
    derive_airzones,
    normalise_name_expr,
    station_type_map,
)

CONFIG = {
    "airzones": [
        "北部空品區",
        "竹苗空品區",
        "中部空品區",
        "雲嘉南空品區",
        "高屏空品區",
        "宜蘭空品區",
        "花東空品區",
        "離島",
    ],
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


class TestAirzoneConflicts:
    """Stations filed under more than one zone, resolved by majority."""

    def test_year_prefix_is_not_swallowed_into_the_zone_name(self) -> None:
        """Folder names are `99年 北部空品區` and `97年中部空品區` — spacing varies.

        A greedy CJK match captures the 年 too, splitting one zone into two.
        """
        frame = _members(
            [
                ("中山", "99年 北部空品區/99年中山站.csv"),
                ("二林", "97年中部空品區/97年二林站.csv"),
            ]
        )

        zones = derive_airzones(frame, CONFIG)

        assert set(zones["airzone"].to_list()) == {"北部空品區", "中部空品區"}

    def test_upstream_misfiling_loses_to_the_majority(self) -> None:
        """臺南 appears once under 北部空品區 in the 1999 package, 23 times under 雲嘉南."""
        frame = _members(
            [("臺南", f"{y}年 雲嘉南空品區/{y}年臺南站.ods") for y in range(82, 105)]
            + [("臺南", "88年 北部空品區/88年臺南站.ods")]
        )

        zones = derive_airzones(frame, CONFIG)

        assert zones["airzone"].to_list() == ["雲嘉南空品區"]

    def test_contested_stations_are_marked_not_silently_resolved(self) -> None:
        frame = _members(
            [
                ("阿里山", "89年中部空品區/89年阿里山站.csv"),
                ("阿里山", "92年中部空品區/92年阿里山站.csv"),
                ("阿里山", "97年 雲嘉南空品區/97年阿里山站.ods"),
            ]
        )

        zones = derive_airzones(frame, CONFIG)

        assert zones["airzone"].to_list() == ["中部空品區"]
        assert zones["airzone_ambiguous"].to_list() == [True]

    def test_unambiguous_stations_are_not_marked(self) -> None:
        frame = _members([("二林", "99年 中部空品區/99年二林站.csv")])

        zones = derive_airzones(frame, CONFIG)

        assert zones["airzone_ambiguous"].to_list() == [False]

    def test_resolution_is_deterministic_regardless_of_input_order(self) -> None:
        rows = [
            ("阿里山", "97年 雲嘉南空品區/97年阿里山站.ods"),
            ("阿里山", "89年中部空品區/89年阿里山站.csv"),
            ("阿里山", "92年中部空品區/92年阿里山站.csv"),
        ]

        first = derive_airzones(_members(rows), CONFIG)["airzone"].to_list()
        second = derive_airzones(_members(list(reversed(rows))), CONFIG)["airzone"].to_list()

        assert first == second == ["中部空品區"]


@pytest.mark.slow
class TestShippedZoneConfig:
    """Scans the whole 341M-row store; deselect with -m 'not slow'."""

    def test_every_station_resolves_to_a_zone(self) -> None:
        """No station may be left without one — a null zone breaks zone analysis."""
        import polars as pl

        from twair.store.stations import build_station_table

        table = build_station_table()

        assert table["airzone"].null_count() == 0
        assert set(table["airzone"].unique()) <= set(load_conf("stations")["airzones"])
        assert table.filter(pl.col("airzone_ambiguous")).height > 0, "ambiguity must stay visible"


@pytest.mark.slow
def test_qc_report_counts_the_same_stations_as_the_station_table() -> None:
    """Both must normalise names, or 台南 and 臺南 are counted as two stations."""
    from twair.qc.report import station_lifecycle  # type: ignore[import-untyped]
    from twair.store.stations import build_station_table

    assert station_lifecycle().height == build_station_table().height
