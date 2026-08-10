"""Tests for station identity, air-quality zone and type resolution."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from twair.config import load_conf
from twair.store import stations as station_store
from twair.store.stations import (
    alias_map,
    attach_geography,
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


def _write_observation_partition(
    root: Path,
    year: int,
    month: int,
    rows: list[tuple[str, datetime, str]],
) -> Path:
    path = root / f"year={year}" / f"month={month:02d}" / "part-0.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame(
        rows,
        schema={
            "station_name": pl.Utf8,
            "ts_local": pl.Datetime("us"),
            "source_member": pl.Utf8,
        },
        orient="row",
    ).write_parquet(path)
    return path


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


class TestPartitionBoundedStationReduction:
    def test_an_empty_observation_root_is_reported_not_turned_into_an_empty_table(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(
            FileNotFoundError,
            match="no observation Parquet partitions",
        ):
            station_store._reduce_station_partitions(tmp_path, CONFIG)

    def test_partition_reduction_preserves_station_years_members_and_zones(
        self,
        tmp_path: Path,
    ) -> None:
        old_member = "99年 雲嘉南空品區/99年台南站_20110329.csv"
        _write_observation_partition(
            tmp_path,
            2010,
            12,
            [
                ("台南", datetime(2010, 12, 31, 22), old_member),
                ("台南", datetime(2010, 12, 31, 23), old_member),
            ],
        )
        _write_observation_partition(
            tmp_path,
            2011,
            1,
            [
                ("臺南", datetime(2011, 1, 1, 0), "臺南_2011.csv"),
                (
                    "前鎮",
                    datetime(2011, 1, 1, 0),
                    "99年 高屏空品區/99年前鎮站_20110329.csv",
                ),
            ],
        )

        station_years, station_members = station_store._reduce_station_partitions(
            tmp_path,
            CONFIG,
        )

        assert station_years.sort("station_name", "year").rows() == [
            ("前鎮", 2011),
            ("臺南", 2010),
            ("臺南", 2011),
        ]
        assert station_members.filter(pl.col("station_name") == "臺南").sort(
            "source_member"
        ).rows() == [
            ("臺南", old_member),
            ("臺南", "臺南_2011.csv"),
        ]
        zones = derive_airzones(station_members, CONFIG)
        assert dict(zip(zones["station_name"], zones["airzone"], strict=True)) == {
            "前鎮": "高屏空品區",
            "臺南": "雲嘉南空品區",
        }

    def test_partition_reduction_reads_one_concrete_parquet_path_at_a_time(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = _write_observation_partition(
            tmp_path,
            2010,
            12,
            [("台南", datetime(2010, 12, 31, 23), "臺南.csv")],
        )
        second = _write_observation_partition(
            tmp_path,
            2011,
            1,
            [("臺南", datetime(2011, 1, 1, 0), "臺南.csv")],
        )
        real_read_parquet = pl.read_parquet
        reads: list[tuple[Path, list[str] | None]] = []

        def record_read(
            source: Path,
            *,
            columns: list[str] | None = None,
        ) -> pl.DataFrame:
            reads.append((Path(source), columns))
            return real_read_parquet(source, columns=columns)

        monkeypatch.setattr(pl, "read_parquet", record_read)

        station_store._reduce_station_partitions(tmp_path, CONFIG)

        assert reads == [
            (path, ["station_name", "ts_local", "source_member"])
            for path in sorted([first, second])
        ]
        assert all(path.name == "part-0.parquet" for path, _ in reads)


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


class TestStationGeography:
    def test_attach_geography_uses_the_resolved_register_and_carries_wanli_provenance(
        self,
    ) -> None:
        table = pl.DataFrame({"station_name": ["萬里"]})

        placed = attach_geography(table)

        assert placed.height == 1
        assert placed["lon"][0] == pytest.approx(121.689881)
        assert placed["lat"][0] == pytest.approx(25.179667)
        assert placed["geo_source"][0] == "reviewed_historical"
        assert placed["geo_source_record_namespace"][0] == "AIRTW central station detail"
        assert placed["geo_source_record_id"][0] == "61"

    def test_the_remaining_unreviewed_stations_keep_null_coordinates(self) -> None:
        names = ["台中", "崇倫", "阿里山", "泰山", "三民"]
        table = pl.DataFrame({"station_name": names})

        unplaced = attach_geography(table)

        assert unplaced.height == len(names)
        assert unplaced["station_name"].to_list() == names
        assert unplaced["lon"].null_count() == len(names)
        assert unplaced["lat"].null_count() == len(names)
        assert unplaced["geo_source"].null_count() == len(names)
        assert unplaced["geo_source_record_namespace"].null_count() == len(names)
        assert unplaced["geo_source_record_id"].null_count() == len(names)
        assert unplaced.filter(pl.col("station_name") == "台中").select(
            "lon", "lat", "geo_source"
        ).row(0) == (None, None, None)


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
    from twair.qc.report import station_lifecycle
    from twair.store.stations import build_station_table

    assert station_lifecycle().height == build_station_table().height
