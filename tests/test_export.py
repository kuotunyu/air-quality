"""Tests for the website data export.

The export's job is to move numbers into a browser without losing what they
mean. Almost every test here is about a distinction surviving the trip: a
withheld aggregate must not arrive as zero, an absent station must not arrive
as a gap in a line that gets interpolated across, and an undocumented test
channel must not arrive at all.
"""

from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from twair.viz import export


@pytest.fixture
def monthly() -> pl.DataFrame:
    """Two stations, three months, with each null case represented once."""
    return pl.DataFrame(
        {
            "station_name": ["三重", "三重", "三重", "楠梓", "楠梓"],
            "pollutant": ["PM2.5"] * 5,
            "month": [
                date(2015, 1, 1),
                date(2015, 2, 1),
                date(2015, 3, 1),
                date(2015, 1, 1),
                date(2015, 3, 1),
            ],
            # 三重 February cleared the threshold but its mean is withheld;
            # 楠梓 February has no row at all.
            "mean": [21.456, None, 18.0, 30.0, None],
            "n_days": [30, 12, 31, 28, 5],
        }
    )


@pytest.fixture
def pollutant_conf() -> dict:
    return {
        "pollutants": {
            "PM2.5": {"name_zh": "細懸浮微粒", "unit": "ug/m3", "valid_range": [0, 1000]},
        }
    }


class TestNullSemantics:
    """The distinction the whole project turns on, at the last hop."""

    def test_a_withheld_mean_arrives_as_null_not_zero(
        self, tmp_path, monkeypatch, monthly, pollutant_conf
    ) -> None:
        monkeypatch.setattr(
            export, "documented_pollutants", lambda config=None: pollutant_conf["pollutants"]
        )

        export.export_l0(tmp_path, monthly=monthly)
        payload = json.loads((tmp_path / "l0" / "pm25.json").read_text(encoding="utf-8"))

        station = payload["stations"].index("三重")
        february = payload["months"].index("2015-02")

        assert payload["mean"][station][february] is None
        assert payload["n_days"][station][february] == 12, (
            "a withheld aggregate keeps its count, so the reader can see it was "
            "suppressed rather than never measured"
        )

    def test_a_station_that_never_reported_is_distinguishable_from_one_that_did(
        self, tmp_path, monkeypatch, monthly, pollutant_conf
    ) -> None:
        monkeypatch.setattr(
            export, "documented_pollutants", lambda config=None: pollutant_conf["pollutants"]
        )

        export.export_l0(tmp_path, monthly=monthly)
        payload = json.loads((tmp_path / "l0" / "pm25.json").read_text(encoding="utf-8"))

        absent = payload["stations"].index("楠梓")
        february = payload["months"].index("2015-02")

        assert payload["mean"][absent][february] is None
        assert payload["n_days"][absent][february] == 0, (
            "no row at all must read as zero periods, which is what tells the "
            "chart to break the line instead of bridging it"
        )

    def test_the_two_null_cases_are_documented_in_the_payload(
        self, tmp_path, monkeypatch, monthly, pollutant_conf
    ) -> None:
        """A consumer should not have to read this repo to decode a null."""
        monkeypatch.setattr(
            export, "documented_pollutants", lambda config=None: pollutant_conf["pollutants"]
        )

        export.export_l0(tmp_path, monthly=monthly)
        payload = json.loads((tmp_path / "l0" / "pm25.json").read_text(encoding="utf-8"))

        assert set(payload["null_means"]) == {"n_days == 0", "n_days > 0"}


class TestShape:
    def test_the_month_axis_is_dense_and_covers_every_month_in_range(
        self, tmp_path, monkeypatch, monthly, pollutant_conf
    ) -> None:
        monkeypatch.setattr(
            export, "documented_pollutants", lambda config=None: pollutant_conf["pollutants"]
        )

        export.export_l0(tmp_path, monthly=monthly)
        payload = json.loads((tmp_path / "l0" / "pm25.json").read_text(encoding="utf-8"))

        assert payload["months"] == ["2015-01", "2015-02", "2015-03"]

    def test_the_axis_crosses_a_year_boundary(self) -> None:
        assert export._month_axis(date(2015, 11, 1), date(2016, 2, 1)) == [
            "2015-11",
            "2015-12",
            "2016-01",
            "2016-02",
        ]

    def test_every_row_of_the_rectangle_is_the_same_length(
        self, tmp_path, monkeypatch, monthly, pollutant_conf
    ) -> None:
        monkeypatch.setattr(
            export, "documented_pollutants", lambda config=None: pollutant_conf["pollutants"]
        )

        export.export_l0(tmp_path, monthly=monthly)
        payload = json.loads((tmp_path / "l0" / "pm25.json").read_text(encoding="utf-8"))

        width = len(payload["months"])
        assert all(len(row) == width for row in payload["mean"])
        assert all(len(row) == width for row in payload["n_days"])
        assert len(payload["mean"]) == len(payload["stations"])


class TestPrecision:
    def test_values_are_rounded_to_the_unit_s_precision(
        self, tmp_path, monkeypatch, monthly, pollutant_conf
    ) -> None:
        monkeypatch.setattr(
            export, "documented_pollutants", lambda config=None: pollutant_conf["pollutants"]
        )

        export.export_l0(tmp_path, monthly=monthly)
        payload = json.loads((tmp_path / "l0" / "pm25.json").read_text(encoding="utf-8"))

        station = payload["stations"].index("三重")
        january = payload["months"].index("2015-01")
        assert payload["mean"][station][january] == 21.46

    def test_precision_follows_the_unit_not_the_pollutant(self) -> None:
        """CO in ppm needs three places where PM2.5 in ug/m3 needs two."""
        assert export._precision({"unit": "ppm"}) == 3
        assert export._precision({"unit": "ug/m3"}) == 2
        assert export._precision({"unit": "something-new"}) == export.DEFAULT_PRECISION


class TestAllowList:
    def test_undocumented_channels_do_not_reach_the_site(
        self, tmp_path, monkeypatch, monthly, pollutant_conf
    ) -> None:
        """The store carries commissioning artefacts such as SO2-test."""
        monkeypatch.setattr(
            export, "documented_pollutants", lambda config=None: pollutant_conf["pollutants"]
        )
        with_junk = pl.concat(
            [
                monthly,
                monthly.head(1).with_columns(pl.lit("SO2-test").alias("pollutant")),
            ]
        )

        result = export.export_l0(tmp_path, monthly=with_junk)

        names = {path.name for path in result.files}
        assert names == {"pm25.json", "index.json"}

    def test_an_empty_allow_list_fails_loudly(self, tmp_path, monkeypatch, monthly) -> None:
        monkeypatch.setattr(export, "documented_pollutants", lambda config=None: {"NOTHING": {}})

        with pytest.raises(RuntimeError, match="nothing to export"):
            export.export_l0(tmp_path, monthly=monthly)


class TestJsonEncoding:
    def test_dates_serialise_as_iso_strings_not_epoch_numbers(self, tmp_path) -> None:
        """An ISO string reads correctly on a chart axis in any timezone."""
        path = export.write_json(tmp_path / "x.json", {"d": date(2015, 3, 1)})

        assert json.loads(path.read_text(encoding="utf-8")) == {"d": "2015-03-01"}

    def test_an_unserialisable_type_raises_rather_than_being_stringified(self, tmp_path) -> None:
        with pytest.raises(TypeError):
            export.write_json(tmp_path / "x.json", {"s": {1, 2}})

    def test_output_carries_no_wasted_whitespace(self, tmp_path) -> None:
        path = export.write_json(tmp_path / "x.json", {"a": [1, 2, 3]})

        assert path.read_text(encoding="utf-8") == '{"a":[1,2,3]}'


class TestManifest:
    def test_every_exported_file_is_checksummed(self, tmp_path) -> None:
        export.write_json(tmp_path / "a.json", {"x": 1})
        export.write_json(tmp_path / "nested" / "b.json", {"y": 2})

        manifest = json.loads(export.write_manifest(tmp_path).read_text(encoding="utf-8"))

        assert {entry["file"] for entry in manifest["files"]} == {"a.json", "nested/b.json"}
        assert all(len(entry["sha256"]) == 64 for entry in manifest["files"])

    def test_the_manifest_does_not_checksum_itself(self, tmp_path) -> None:
        export.write_json(tmp_path / "a.json", {"x": 1})
        export.write_manifest(tmp_path)

        manifest = json.loads(export.write_manifest(tmp_path).read_text(encoding="utf-8"))

        assert [entry["file"] for entry in manifest["files"]] == ["a.json"]


class TestSlugs:
    def test_a_dot_in_a_pollutant_code_does_not_become_a_file_extension(self) -> None:
        assert export._slug("PM2.5") == "pm25"

    def test_slugs_cannot_escape_their_directory(self) -> None:
        assert "/" not in export._slug("NO/NO2")
