"""The quality report, which is one of the four things this project publishes.

`qc/report.py` is the module that answers the 2018 project's one-sentence
dismissal of its data problems — 「本專題將有遺漏值之資料以鄰近測站之資料代替」 —
by measuring every quality property instead. It writes `data/outputs/qc/` and
`docs/data-quality.md`, and it was 83 statements at 0% coverage.

Three of its properties are load-bearing for arguments made elsewhere on the
site, and none had been executed:

  * **Name normalisation.** `_base`'s own docstring says that without it 台南
    and 臺南 are two stations, and every per-station statistic splits at the
    year MOENV changed the spelling. That is a silent halving of a station's
    record, not an error.
  * **Retention asymmetry.** Chapter 8 argues from it: pre-2018 archives keep
    the number behind an invalidation flag and later ones discard it, so the
    denominator of any long-run imputation moves.
  * **Sentinel rates.** The comment above `SENTINEL_FLAGS` records the list
    being written by hand once and undercounting by 75% until a regenerated
    report happened to show it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from twair.qc import report as qc_report_module
from twair.qc.flags import Flag
from twair.qc.report import (
    REPORTS,
    _md_table,
    build_reports,
    coverage_by_year,
    pm_pair_diagnostics,
    retention_asymmetry,
    run_report,
    sentinel_rates,
    station_lifecycle,
    write_markdown,
)
from twair.store.schema import PARTITION_SCHEMA, conform_partition


def row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "station_name": "臺南",
        "pollutant": "PM2.5",
        "ts_local": datetime(2015, 6, 1, 3),
        "value": 21.0,
        "flag": Flag.VALID.value,
        "value_retained": True,
        "imputed": False,
        "impute_method": None,
        "generation": "gen2",
        "source_member": "x.csv",
    }
    base.update(over)
    return base


Store = Callable[[list[dict[str, object]]], Path]


def publication_event_config() -> dict[str, object]:
    return {
        "events": [
            {
                "event_id": "wanli_monitoring_stop_2025",
                "station_name": "萬里",
                "event_kind": "monitoring_stop",
                "effective_from": "2025-05-01T00:00:00+08:00",
                "source_url": "https://example.invalid/official-notice",
                "source_published_on": "2025-04-30",
                "source_statement": "data will no longer update",
            }
        ]
    }


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Write rows into a partitioned tree the way the real store is laid out."""

    def build(rows: list[dict[str, object]]) -> Path:
        frame = conform_partition(pl.DataFrame(rows).select(list(PARTITION_SCHEMA)))
        part = tmp_path / "obs"
        part.mkdir(exist_ok=True)
        frame.write_parquet(part / "p.parquet")
        return part

    return build


# ── name normalisation ───────────────────────────────────────────────────────


def test_the_two_spellings_of_tainan_are_one_station(store: Store) -> None:
    """The failure `_base` exists to prevent, and it is a silent one.

    MOENV changed 台南 to 臺南 partway through the record. Counted naively that
    is two stations, each with half a history, and every per-station statistic
    — coverage, lifecycle, ranking — splits at the year of the change without
    anything looking wrong.
    """
    root = store(
        [
            row(station_name="台南", ts_local=datetime(2005, 1, 1, 0)),
            row(station_name="臺南", ts_local=datetime(2015, 1, 1, 0)),
        ]
    )

    life = station_lifecycle(root)

    assert life.height == 1, f"expected one station, got {life['station_name'].to_list()}"
    assert life["station_name"][0] == "臺南"


def test_coverage_counts_the_aliased_station_once_per_year(store: Store) -> None:
    root = store(
        [
            row(station_name="台南", ts_local=datetime(2015, 1, 1, 0)),
            row(station_name="臺南", ts_local=datetime(2015, 6, 1, 0)),
        ]
    )

    coverage = coverage_by_year(root)

    assert coverage.height == 1
    assert coverage["stations"][0] == 1, "an alias was counted as a second station"
    assert coverage["rows"][0] == 2


# ── the ratios the report publishes ──────────────────────────────────────────


def test_valid_and_missing_ratios_are_fractions_of_the_year(store: Store) -> None:
    root = store(
        [
            row(),
            row(ts_local=datetime(2015, 6, 1, 4), flag=Flag.MISSING.value, value=None),
            row(ts_local=datetime(2015, 6, 1, 5), flag=Flag.INSTRUMENT_CHECK_INVALID.value),
            row(ts_local=datetime(2015, 6, 1, 6), flag=Flag.VALID.value),
        ]
    )

    coverage = coverage_by_year(root)

    assert coverage["rows"][0] == 4
    assert coverage["valid"][0] == 2
    assert coverage["missing"][0] == 1
    assert coverage["invalid"][0] == 1
    assert coverage["valid_ratio"][0] == pytest.approx(0.5)
    assert coverage["missing_ratio"][0] == pytest.approx(0.25)


def test_retention_asymmetry_separates_the_two_archive_generations(store: Store) -> None:
    """The number chapter 8 argues from.

    A rejected reading in a pre-2018 file keeps its value; in a later file the
    value is gone. Reported per generation, because a single average across the
    record would hide exactly the change that matters.
    """
    root = store(
        [
            # gen2: rejected, value kept
            row(generation="gen2", flag=Flag.INSTRUMENT_CHECK_INVALID.value, value_retained=True),
            row(
                generation="gen2",
                ts_local=datetime(2015, 6, 1, 4),
                flag=Flag.MANUAL_CHECK_INVALID.value,
                value_retained=True,
            ),
            # gen3: rejected, value discarded
            row(
                generation="gen3",
                ts_local=datetime(2015, 6, 1, 5),
                flag=Flag.INSTRUMENT_CHECK_INVALID.value,
                value_retained=False,
            ),
            # a valid row, which must not appear at all
            row(ts_local=datetime(2015, 6, 1, 6), generation="gen3"),
        ]
    )

    asym = retention_asymmetry(root)

    by_gen = {r["generation"]: r for r in asym.to_dicts()}
    assert set(by_gen) == {"gen2", "gen3"}, "a valid row leaked into the invalid-only table"
    assert by_gen["gen2"]["invalid"] == 2
    assert by_gen["gen2"]["retained_ratio"] == pytest.approx(1.0)
    assert by_gen["gen3"]["invalid"] == 1
    assert by_gen["gen3"]["retained_ratio"] == pytest.approx(0.0)


def test_sentinel_rates_count_every_flag_the_pass_can_emit(store: Store) -> None:
    """The list was hand-written once and undercounted by 75%.

    `SENTINEL_FLAGS` covers calm, variable direction and instrument fault. A
    report that counts only one of them looks like a report; it just answers a
    smaller question than its heading claims.
    """
    root = store(
        [
            row(pollutant="WIND_DIREC", flag=Flag.CALM.value, value=None),
            row(
                pollutant="WIND_DIREC",
                ts_local=datetime(2015, 6, 1, 4),
                flag=Flag.VARIABLE_DIRECTION.value,
                value=None,
            ),
            row(
                pollutant="WIND_DIREC",
                ts_local=datetime(2015, 6, 1, 5),
                flag=Flag.INSTRUMENT_FAULT.value,
                value=None,
            ),
            row(pollutant="WIND_DIREC", ts_local=datetime(2015, 6, 1, 6)),
        ]
    )

    rates = sentinel_rates(root)

    flags = set(rates["flag"].to_list())
    assert flags == {
        Flag.CALM.value,
        Flag.VARIABLE_DIRECTION.value,
        Flag.INSTRUMENT_FAULT.value,
    }, f"a sentinel flag went uncounted: {flags}"
    assert int(rates["n"].sum()) == 3, "the valid reading was counted as a sentinel"


# ── the PM pair, and the pairs that were being thrown away ───────────────────


def pair(hour: int, pm25: float, pm10: float) -> list[dict[str, object]]:
    """One hour at one station reporting both particle sizes."""
    at = datetime(2015, 6, 1, hour)
    return [
        row(pollutant="PM2.5", ts_local=at, value=pm25),
        row(pollutant="PM10", ts_local=at, value=pm10),
    ]


def test_a_pm25_above_pm10_is_counted_as_impossible(store: Store) -> None:
    """PM2.5 is a physical subset of PM10, so the pair cannot both be right."""
    rows = pair(0, 30.0, 40.0) + pair(1, 45.0, 40.0) + pair(2, 20.0, 20.0)
    frame = pm_pair_diagnostics(store(rows))

    assert frame["paired_hours"][0] == 3
    assert frame["impossible"][0] == 1
    assert frame["identical"][0] == 1
    assert frame["impossible_rate"][0] == pytest.approx(1 / 3, abs=1e-5)


def test_a_pm10_of_zero_beside_a_real_pm25_is_still_impossible(store: Store) -> None:
    """The regression this test was written for.

    `PM2.5 / PM10` is undefined at PM10 = 0, and the guard against that used to
    be a filter applied before the counting. So the single most impossible pair
    in the archive — 萬里, 2004-10-27, PM10 reading 0.0 against PM2.5 reading
    37.0 — was dropped from the count of impossible pairs. Across the store that
    was 12,209 of 355,209, all of them at the extreme end. A rate computed by
    discarding its own strongest evidence is not a rate.
    """
    rows = pair(0, 30.0, 40.0) + pair(1, 37.0, 0.0)
    frame = pm_pair_diagnostics(store(rows))

    assert frame["paired_hours"][0] == 2, "the zero-PM10 hour vanished from the denominator"
    assert frame["impossible"][0] == 1, "the most impossible pair in the file went uncounted"
    assert frame["zero_pm10"][0] == 1, "the exclusion is reported, not absorbed"


def test_the_ratio_quantiles_skip_the_undefined_pairs(store: Store) -> None:
    """Counted in the open, but not divided by — an infinite ratio is not data."""
    rows = pair(0, 20.0, 40.0) + pair(1, 30.0, 60.0) + pair(2, 37.0, 0.0)
    frame = pm_pair_diagnostics(store(rows))

    assert frame["ratio_median"][0] == pytest.approx(0.5), "a zero denominator reached the median"
    assert frame["ratio_p95"][0] == pytest.approx(0.5)


def test_a_store_with_no_overlapping_hours_returns_an_empty_frame(store: Store) -> None:
    """PM2.5 monitoring began years after PM10; the early years have no pairs."""
    rows = [
        row(pollutant="PM10", ts_local=datetime(1995, 6, 1, 0), value=40.0),
        row(pollutant="PM2.5", ts_local=datetime(2015, 6, 1, 0), value=20.0),
    ]
    frame = pm_pair_diagnostics(store(rows))

    assert frame.is_empty()
    assert "paired_hours" in frame.columns, "an empty frame still has to have the shape"


@pytest.mark.parametrize("only", ["PM2.5", "PM10"])
def test_a_store_holding_one_particle_size_is_an_answer_not_a_crash(
    store: Store, only: str
) -> None:
    """The pivot invents no column for a value it never saw.

    A store with PM10 and no PM2.5 is an ordinary state — the network measured
    PM10 for years before PM2.5 existed — and it used to raise
    ColumnNotFoundError here, which aborted `build_reports` and took the seven
    unrelated reports in the same loop down with it.
    """
    frame = pm_pair_diagnostics(store([row(pollutant=only, value=30.0)]))

    assert frame.is_empty()
    assert frame.columns == ["obs_year", "paired_hours"]


# ── reviewed publication events ──────────────────────────────────────────────


def test_a_numeric_value_after_an_official_event_is_counted_as_published(
    store: Store,
) -> None:
    root = store(
        [
            row(station_name="萬里", ts_local=datetime(2025, 4, 30, 23), value=12.0),
            row(station_name="萬里", ts_local=datetime(2025, 5, 1, 1), value=13.0),
        ]
    )

    frame = qc_report_module.station_publication_conflicts(root, config=publication_event_config())
    result = frame.row(0, named=True)

    assert frame.schema == qc_report_module.PUBLICATION_EVENT_COLUMNS
    assert result["rows_at_or_after_event"] == 1
    assert result["numeric_rows_at_or_after_event"] == 1
    assert result["null_rows_at_or_after_event"] == 0
    assert result["published_after_event"] is True


def test_a_null_after_an_official_event_stays_null_and_is_not_numeric(store: Store) -> None:
    root = store(
        [
            row(station_name="萬里", pollutant="PM10", ts_local=datetime(2025, 4, 30, 23)),
            row(
                station_name="萬里",
                pollutant="PM10",
                ts_local=datetime(2025, 5, 1, 1),
                value=None,
                flag=Flag.MISSING.value,
            ),
        ]
    )

    result = qc_report_module.station_publication_conflicts(
        root, config=publication_event_config()
    ).row(0, named=True)

    assert result["rows_at_or_after_event"] == 1
    assert result["numeric_rows_at_or_after_event"] == 0
    assert result["null_rows_at_or_after_event"] == 1
    assert result["published_after_event"] is False


def test_a_known_pollutant_with_no_post_event_rows_produces_a_zero_count_finding(
    store: Store,
) -> None:
    root = store(
        [
            row(
                station_name="萬里",
                pollutant="CO",
                ts_local=datetime(2025, 4, 30, 23),
            )
        ]
    )

    result = qc_report_module.station_publication_conflicts(
        root, config=publication_event_config()
    ).row(0, named=True)

    assert result["pollutant"] == "CO"
    assert result["rows_at_or_after_event"] == 0
    assert result["numeric_rows_at_or_after_event"] == 0
    assert result["first_post_event_ts"] is None
    assert result["last_post_event_ts"] is None
    assert result["published_after_event"] is False


def test_publication_event_measurement_never_changes_the_canonical_partition(
    store: Store,
) -> None:
    root = store(
        [
            row(station_name="萬里", ts_local=datetime(2025, 4, 30, 23), value=12.0),
            row(station_name="萬里", ts_local=datetime(2025, 5, 1, 1), value=None),
        ]
    )
    partition = root / "p.parquet"
    before = pl.read_parquet(partition)

    qc_report_module.station_publication_conflicts(root, config=publication_event_config())

    assert_frame_equal(pl.read_parquet(partition), before, check_row_order=True)


def test_the_full_report_reduces_one_concrete_partition_at_a_time(
    tmp_path: Path,
    elsewhere: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "partitioned"
    for month, rows in (
        (1, [row(ts_local=datetime(2024, 1, 1, 0))]),
        (2, [row(ts_local=datetime(2024, 2, 1, 0))]),
    ):
        destination = root / "year=2024" / f"month={month:02d}"
        destination.mkdir(parents=True)
        conform_partition(pl.DataFrame(rows).select(list(PARTITION_SCHEMA))).write_parquet(
            destination / "part.parquet"
        )

    def whole_store_scan_is_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the QC report returned to a whole-store lazy scan")

    reads: list[tuple[Path, tuple[str, ...]]] = []
    read_parquet = pl.read_parquet

    def record_partition_read(
        source: str | Path, *, columns: list[str] | None = None, **kwargs: Any
    ) -> pl.DataFrame:
        assert isinstance(source, Path), "a glob or path list can retain every partition at once"
        assert columns is not None, "every report must project only the columns it measures"
        reads.append((source, tuple(columns)))
        return read_parquet(source, columns=columns, **kwargs)

    monkeypatch.setattr(
        qc_report_module, "scan_observations", whole_store_scan_is_forbidden, raising=False
    )
    monkeypatch.setattr(pl, "scan_parquet", whole_store_scan_is_forbidden)
    monkeypatch.setattr(pl, "read_parquet", record_partition_read)

    results = build_reports(root)

    assert set(results) == set(REPORTS)
    assert {path for path, _ in reads} == set(root.glob("**/*.parquet"))


def test_the_public_report_calls_the_result_an_unresolved_source_disagreement(
    store: Store, elsewhere: Path
) -> None:
    root = store(
        [
            row(station_name="萬里", ts_local=datetime(2025, 4, 30, 23), value=12.0),
            row(station_name="萬里", ts_local=datetime(2025, 5, 1, 1), value=13.0),
        ]
    )
    results = build_reports(root)
    results["station_publication_conflicts"] = qc_report_module.station_publication_conflicts(
        root, config=publication_event_config()
    )

    text = write_markdown(results).read_text(encoding="utf-8")

    assert "來源之間尚未釐清的差異" in text
    assert "不是資料有效性的判定" in text
    assert "不刪除、不補值，也不改寫" in text


# ── the markdown renderer ────────────────────────────────────────────────────


def test_a_truncated_table_says_it_was_truncated() -> None:
    """A table silently cut at 60 rows reads as the whole answer."""
    frame = pl.DataFrame({"obs_year": list(range(1990, 1995))})
    rendered = _md_table(frame, ["obs_year"], limit=3)

    assert rendered.count("\n| ") == 3, "the row limit was not applied"
    assert "共 5 列" in rendered, "the reader was not told rows were withheld"
    assert "1990" in rendered and "1994" not in rendered


def test_an_untruncated_table_does_not_apologise() -> None:
    frame = pl.DataFrame({"obs_year": [1990, 1991]})
    assert "顯示前" not in _md_table(frame, ["obs_year"], limit=60)


def test_a_null_renders_as_a_blank_cell_not_the_word_none() -> None:
    """`str(None)` is "None", which in a published table reads as a value."""
    frame = pl.DataFrame({"a": [1, None]}, schema={"a": pl.Int64})
    assert "None" not in _md_table(frame, ["a"])


# ── the two published artefacts ──────────────────────────────────────────────


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both output paths, which are module constants, not arguments.

    `build_reports` writes to `data/outputs/qc/` and `write_markdown` to
    `docs/data-quality.md` — the real ones, committed to this repository. A test
    that ran them unredirected would rewrite the published report from four
    fixture rows.
    """
    out = tmp_path / "out"
    docs = tmp_path / "docs"
    monkeypatch.setattr("twair.qc.report.outputs_dir", lambda *_: out / "qc")
    monkeypatch.setattr("twair.qc.report.DOCS_DIR", docs)
    return tmp_path


def test_every_report_in_the_index_is_written_to_disk(store: Store, elsewhere: Path) -> None:
    """The index and the files are two lists that have to stay the same list."""
    results = build_reports(store([row(), row(pollutant="PM10", value=40.0)]))

    written = {p.stem for p in (elsewhere / "out" / "qc").glob("*.parquet")}
    assert written == set(REPORTS), f"index and disk disagree: {written ^ set(REPORTS)}"
    assert set(results) == set(REPORTS)


def test_the_overview_numbers_come_from_the_data(store: Store, elsewhere: Path) -> None:
    root = store(
        [
            row(ts_local=datetime(2001, 1, 1, 0)),
            row(ts_local=datetime(2015, 6, 1, 4), flag=Flag.MISSING.value, value=None),
            row(station_name="古亭", ts_local=datetime(2015, 6, 1, 5)),
            row(station_name="古亭", ts_local=datetime(2015, 6, 1, 6)),
        ]
    )
    doc = write_markdown(build_reports(root))
    text = doc.read_text(encoding="utf-8")

    assert "| 涵蓋年份 | 2001–2015 |" in text
    assert "| 總觀測筆數 | 4 |" in text
    assert "| 測站數（歷來） | 2 |" in text
    assert "| 整體有效值比例 | 75.00% |" in text


def test_an_absent_sentinel_is_stated_rather_than_left_blank(store: Store, elsewhere: Path) -> None:
    """The Phase 0 question was whether 888/999 appear at all in 2010–2017.

    "No sentinels in this period" and "this report did not look" render the
    same way if the empty case just prints an empty table.
    """
    doc = write_markdown(build_reports(store([row()])))
    text = doc.read_text(encoding="utf-8")

    assert "未出現" in text
    assert "_無無效值紀錄。_" in text, "the empty invalid table went unlabelled too"


def test_the_sentinel_rate_is_sentinels_over_wind_cells(store: Store, elsewhere: Path) -> None:
    """Two reports divided by each other, and only here.

    `sentinel_rates` counts the flagged cells and `wind_direction_totals` counts
    every wind cell; neither knows about the other. The published rate is the
    join of the two, so a mismatched key or a missing year would print a number
    that is not a fraction of anything.
    """
    hours = [row(pollutant="WIND_DIREC", ts_local=datetime(2003, 1, 1, h)) for h in range(4)]
    hours[0]["flag"] = Flag.CALM.value
    doc = write_markdown(build_reports(store(hours)))
    text = doc.read_text(encoding="utf-8")

    assert "| 2003 | 1 | 4 | 0.25 |" in text, "the sentinel rate is not 1 in 4"


def test_run_report_writes_both_artefacts(store: Store, elsewhere: Path) -> None:
    """The CLI entry point, which is the only way either file is ever produced."""
    run_report(store([row()]))

    assert (elsewhere / "out" / "qc" / "coverage_by_year.parquet").exists()
    assert (elsewhere / "docs" / "data-quality.md").exists()
