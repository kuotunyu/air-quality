"""Tests for the website data export.

The export's job is to move numbers into a browser without losing what they
mean. Almost every test here is about a distinction surviving the trip: a
withheld aggregate must not arrive as zero, an absent station must not arrive
as a gap in a line that gets interpolated across, and an undocumented test
channel must not arrive at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from scripts import check_web_export

from twair.viz import export


def _run_check(root: Path) -> int:
    """Run the shipped-export check over `root`, the way CI runs it."""
    argv = sys.argv
    sys.argv = ["check_web_export.py", str(root)]
    try:
        return check_web_export.main()
    finally:
        sys.argv = argv


def _meta_stations() -> pl.DataFrame:
    names = ["萬里", "台中", "崇倫", "阿里山", "泰山", "三民"]
    return pl.DataFrame(
        {
            "station_name": names,
            "county": ["新北市", None, None, None, None, None],
            "township": ["萬里區", None, None, None, None, None],
            "lon": [121.689881, None, None, None, None, None],
            "lat": [25.179667, None, None, None, None, None],
            "station_name_en": ["Wanli", None, None, None, None, None],
            "geo_source": ["reviewed_historical", None, None, None, None, None],
            "geo_source_record_namespace": [
                "AIRTW central station detail",
                None,
                None,
                None,
                None,
                None,
            ],
            "geo_source_record_id": ["61", None, None, None, None, None],
            "airzone": ["北部空品區"] * 6,
            "station_type": ["background"] * 6,
            "first_year": [1993, 1984, 1999, 2000, 2005, 1983],
            "last_year": [2025, 1992, 2011, 2011, 2009, 1999],
            "years_present": [33, 9, 13, 11, 5, 17],
        }
    )


def _publication_conflict() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "event_id": ["wanli_monitoring_stop_2025"],
            "station_name": ["萬里"],
            "event_kind": ["monitoring_stop"],
            "effective_from": ["2025-05-01T00:00:00+08:00"],
            "source_url": ["https://example.invalid/official-notice"],
            "source_published_on": ["2025-04-30"],
            "source_statement": ["data will no longer update"],
            "pollutant": ["PM2.5"],
            "rows_at_or_after_event": [5880],
            "numeric_rows_at_or_after_event": [3894],
            "null_rows_at_or_after_event": [1986],
            "first_post_event_ts": [datetime(2025, 5, 1, 0)],
            "last_post_event_ts": [datetime(2025, 12, 31, 23)],
            "published_after_event": [True],
        }
    )


def _stub_meta_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stations: pl.DataFrame | None = None,
    conflicts: pl.DataFrame | None = None,
) -> None:
    selected_stations = stations if stations is not None else _meta_stations()
    monkeypatch.setattr(export, "_read_stations", lambda: selected_stations)
    monkeypatch.setattr(export, "_read_publication_conflicts", lambda: conflicts)
    monkeypatch.setattr(export, "documented_pollutants", lambda config=None: {})
    monkeypatch.setattr(export, "_data_through", lambda: "2025-12-31 23:00:00")
    monkeypatch.setattr(export, "_hourly_observations", lambda: 1)
    monkeypatch.setattr(export, "git_state", lambda: ("abc1234", False))


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
def pollutant_conf() -> dict[str, Any]:
    return {
        "pollutants": {
            "PM2.5": {"name_zh": "細懸浮微粒", "unit": "ug/m3", "valid_range": [0, 1000]},
        }
    }


class TestNullSemantics:
    """The distinction the whole project turns on, at the last hop."""

    def test_a_withheld_mean_arrives_as_null_not_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        monthly: pl.DataFrame,
        pollutant_conf: dict[str, Any],
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        monthly: pl.DataFrame,
        pollutant_conf: dict[str, Any],
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        monthly: pl.DataFrame,
        pollutant_conf: dict[str, Any],
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        monthly: pl.DataFrame,
        pollutant_conf: dict[str, Any],
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        monthly: pl.DataFrame,
        pollutant_conf: dict[str, Any],
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        monthly: pl.DataFrame,
        pollutant_conf: dict[str, Any],
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

    def test_every_unit_the_config_declares_has_a_precision(self) -> None:
        """The fallback is silent, and for one unit it is wrong.

        An unmapped unit publishes at `DEFAULT_PRECISION`, two places. That is
        right for most of them and wrong for ppm, where CO spends its whole
        range: 0.42 ppm instead of 0.418. Adding a pollutant, or respelling a
        unit as `µg/m3`, would do that without any error anywhere — the number
        would just quietly get coarser on the site.
        """
        units = {str(meta.get("unit", "")) for meta in export.documented_pollutants().values()}

        unmapped = sorted(units - set(export.PRECISION_BY_UNIT))

        assert not unmapped, f"declared in conf/pollutants.yaml, no precision: {unmapped}"

    def test_the_precision_table_has_no_units_the_config_does_not_use(self) -> None:
        """The other direction, which is how a respelling shows up as a pair."""
        units = {str(meta.get("unit", "")) for meta in export.documented_pollutants().values()}

        orphaned = sorted(set(export.PRECISION_BY_UNIT) - units)

        assert not orphaned, f"a precision for a unit nothing declares: {orphaned}"


class TestAllowList:
    def test_undocumented_channels_do_not_reach_the_site(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        monthly: pl.DataFrame,
        pollutant_conf: dict[str, Any],
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

    def test_an_empty_allow_list_fails_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, monthly: pl.DataFrame
    ) -> None:
        monkeypatch.setattr(export, "documented_pollutants", lambda config=None: {"NOTHING": {}})

        with pytest.raises(RuntimeError, match="nothing to export"):
            export.export_l0(tmp_path, monthly=monthly)


class TestJsonEncoding:
    def test_dates_serialise_as_iso_strings_not_epoch_numbers(self, tmp_path: Path) -> None:
        """An ISO string reads correctly on a chart axis in any timezone."""
        path = export.write_json(tmp_path / "x.json", {"d": date(2015, 3, 1)})

        assert json.loads(path.read_text(encoding="utf-8")) == {"d": "2015-03-01"}

    def test_an_unserialisable_type_raises_rather_than_being_stringified(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(TypeError):
            export.write_json(tmp_path / "x.json", {"s": {1, 2}})

    def test_output_carries_no_wasted_whitespace(self, tmp_path: Path) -> None:
        path = export.write_json(tmp_path / "x.json", {"a": [1, 2, 3]})

        assert path.read_text(encoding="utf-8") == '{"a":[1,2,3]}'


class TestManifest:
    def test_every_exported_file_is_checksummed(self, tmp_path: Path) -> None:
        export.write_json(tmp_path / "a.json", {"x": 1})
        export.write_json(tmp_path / "nested" / "b.json", {"y": 2})

        manifest = json.loads(export.write_manifest(tmp_path).read_text(encoding="utf-8"))

        assert {entry["file"] for entry in manifest["files"]} == {"a.json", "nested/b.json"}
        assert all(len(entry["sha256"]) == 64 for entry in manifest["files"])

    def test_the_manifest_does_not_checksum_itself(self, tmp_path: Path) -> None:
        export.write_json(tmp_path / "a.json", {"x": 1})
        export.write_manifest(tmp_path)

        manifest = json.loads(export.write_manifest(tmp_path).read_text(encoding="utf-8"))

        assert [entry["file"] for entry in manifest["files"]] == ["a.json"]


class TestPublicationBoundary:
    def test_meta_exports_geography_provenance_and_only_the_five_unresolved_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_meta_dependencies(monkeypatch)

        payload = json.loads(export.export_meta(tmp_path).read_text(encoding="utf-8"))

        wanli = next(row for row in payload["stations"] if row["station_name"] == "萬里")
        assert wanli["geo_source"] == "reviewed_historical"
        assert wanli["geo_source_record_namespace"] == "AIRTW central station detail"
        assert wanli["geo_source_record_id"] == "61"
        assert payload["stations_without_coordinates"] == ["台中", "崇倫", "阿里山", "泰山", "三民"]

    def test_meta_exports_the_measured_publication_conflict_without_recomputing_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conflict = _publication_conflict()
        _stub_meta_dependencies(monkeypatch, conflicts=conflict)

        summary = json.loads(export.export_meta(tmp_path).read_text(encoding="utf-8"))[
            "station_publication_conflicts"
        ]

        assert summary["status"] == "available"
        assert summary["records"] == [
            {
                **conflict.row(0, named=True),
                "first_post_event_ts": "2025-05-01T00:00:00",
                "last_post_event_ts": "2025-12-31T23:00:00",
            }
        ]

    def test_meta_marks_a_missing_conflict_artifact_unavailable_instead_of_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_meta_dependencies(monkeypatch, conflicts=None)

        summary = json.loads(export.export_meta(tmp_path).read_text(encoding="utf-8"))[
            "station_publication_conflicts"
        ]

        assert summary == {
            "status": "unavailable",
            "reason": "qc_artifact_not_available",
        }
        assert "records" not in summary

    def test_meta_describes_l2_as_local_and_rebuildable_not_remotely_hosted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stations = pl.DataFrame(
            {
                "station_name": ["甲站"],
                "airzone": ["北部空品區"],
                "station_type": ["general"],
                "first_year": [2025],
                "last_year": [2025],
                "years_present": [1],
                "geo_source": [None],
                "geo_source_record_namespace": [None],
                "geo_source_record_id": [None],
            }
        )
        _stub_meta_dependencies(monkeypatch, stations=stations)

        path = export.export_meta(tmp_path)
        layer = json.loads(path.read_text(encoding="utf-8"))["layers"]["L2"]

        assert "local" in layer.lower()
        assert "pipeline" in layer.lower()
        assert "huggingface" not in layer.lower()


class TestShippedExportCheck:
    """`scripts/check_web_export.py` — the manifest against the tree, not a tmp dir.

    Both tests above pass a directory this function just populated, so they can
    only ever confirm that `write_manifest` describes its own output. The defect
    was in the gap between two runs: two story payloads were regenerated and
    committed while the manifest still described the export before them.
    """

    def _tree(self, root: Path, *, files: dict[str, Any], measured: bool = True) -> None:
        for name, payload in files.items():
            export.write_json(root / name, payload)
        export.write_json(
            root / "meta.json", {"hourly_observations": 341_442_552 if measured else None}
        )
        export.write_manifest(root)

    def test_a_complete_export_passes(self, tmp_path: Path) -> None:
        self._tree(tmp_path, files={"story/a.json": {"x": 1}})

        assert _run_check(tmp_path) == 0

    def test_a_payload_written_after_the_manifest_fails(self, tmp_path: Path) -> None:
        """This is exactly what shipped: the file is there, the checksum is not."""
        self._tree(tmp_path, files={"story/a.json": {"x": 1}})
        export.write_json(tmp_path / "story" / "late.json", {"y": 2})

        assert _run_check(tmp_path) == 1

    def test_a_payload_edited_after_export_fails(self, tmp_path: Path) -> None:
        self._tree(tmp_path, files={"story/a.json": {"x": 1}})
        export.write_json(tmp_path / "story" / "a.json", {"x": 2})

        assert _run_check(tmp_path) == 1

    def test_a_listed_file_absent_from_this_tree_is_not_a_failure(self, tmp_path: Path) -> None:
        """Most of `l1/` is gitignored, so a clean checkout carries a subset.

        Treating that as a failure would make the check unrunnable in CI, which
        is the one place it has to run.
        """
        self._tree(tmp_path, files={"story/a.json": {"x": 1}, "l1/big.json": {"z": 3}})
        (tmp_path / "l1" / "big.json").unlink()

        assert _run_check(tmp_path) == 0

    def test_a_meta_without_a_measured_row_count_fails(self, tmp_path: Path) -> None:
        """The site falls back to a declared constant, so nothing else would notice."""
        self._tree(tmp_path, files={"story/a.json": {"x": 1}}, measured=False)

        assert _run_check(tmp_path) == 1

    def test_an_unreachable_commit_object_is_not_valid_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=repository-owner",
                    "-c",
                    "user.email=owner@example.invalid",
                    *args,
                ],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )

        git("init", "-b", "old-history")
        (repo / "old.txt").write_text("old history\n", encoding="utf-8")
        git("add", "old.txt")
        git("commit", "-m", "old history")
        stale_sha = git("rev-parse", "HEAD").stdout.strip()

        git("switch", "--orphan", "rewritten-history")
        (repo / "old.txt").unlink(missing_ok=True)
        (repo / "current.txt").write_text("current history\n", encoding="utf-8")
        git("add", "--all")
        git("commit", "-m", "rewritten history")
        git("branch", "-D", "old-history")

        data = repo / "web" / "public" / "data"
        self._tree(data, files={"story/a.json": {"x": 1}})
        manifest_path = data / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["git_sha"] = stale_sha
        export.write_json(manifest_path, manifest)

        assert git("cat-file", "-e", f"{stale_sha}^{{commit}}").returncode == 0
        assert (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", stale_sha, "HEAD"],
                cwd=repo,
                check=False,
            ).returncode
            == 1
        )
        monkeypatch.chdir(repo)

        assert _run_check(data) == 1


class TestSlugs:
    def test_a_dot_in_a_pollutant_code_does_not_become_a_file_extension(self) -> None:
        assert export._slug("PM2.5") == "pm25"

    def test_slugs_cannot_escape_their_directory(self) -> None:
        assert "/" not in export._slug("NO/NO2")
