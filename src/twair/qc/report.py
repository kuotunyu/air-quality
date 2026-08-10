"""Data quality reporting over the canonical store.

The 2018 project disposed of its data-quality problems in one sentence:
「本專題將有遺漏值之資料以鄰近測站之資料代替」. What follows is the opposite
approach — every quality property is measured, published, and left visible for
the analysis stage to reason about.

Outputs go to ``data/outputs/qc/`` as Parquet (for the website and further
analysis) plus ``docs/data-quality.md`` as a human-readable summary.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl
from rich.console import Console

from twair.config import ConfigError, load_conf
from twair.paths import DOCS_DIR, outputs_dir, processed_dir
from twair.qc.flags import Flag
from twair.qc.sentinels import SENTINEL_FLAGS
from twair.scalars import as_float, as_int
from twair.store.stations import normalise_name_expr
from twair.store.writer import OBSERVATIONS

log = logging.getLogger(__name__)
console = Console()

INVALID_FLAGS = (
    Flag.INSTRUMENT_CHECK_INVALID.value,
    Flag.PROGRAM_CHECK_INVALID.value,
    Flag.MANUAL_CHECK_INVALID.value,
)


def _base(root: Path | None, *, columns: list[str]) -> Iterator[pl.DataFrame]:
    """Scan the store with station names normalised and the year derived.

    Normalisation matters here as much as anywhere: without it 台南 and 臺南
    are counted as two stations, and every per-station statistic splits at the
    year MOENV changed the spelling.
    """
    target = root or processed_dir(OBSERVATIONS)
    paths = sorted(target.glob("**/*.parquet"))
    if not paths:
        raise FileNotFoundError(f"no observation Parquet partitions under {target}")
    projected = list(dict.fromkeys([*columns, "station_name", "ts_local"]))
    for path in paths:
        yield pl.read_parquet(path, columns=projected).with_columns(
            normalise_name_expr(),
            pl.col("ts_local").dt.year().cast(pl.Int16).alias("obs_year"),
        )


def coverage_by_year(root: Path | None = None) -> pl.DataFrame:
    """Rows, stations, pollutants and valid fraction per year."""
    counts: list[pl.DataFrame] = []
    station_years: list[pl.DataFrame] = []
    pollutant_years: list[pl.DataFrame] = []
    generation_years: list[pl.DataFrame] = []
    for part in _base(
        root,
        columns=["pollutant", "flag", "value_retained", "generation"],
    ):
        counts.append(
            part.group_by("obs_year").agg(
                pl.len().alias("rows"),
                (pl.col("flag") == Flag.VALID.value).sum().alias("valid"),
                pl.col("flag").is_in(INVALID_FLAGS).sum().alias("invalid"),
                (pl.col("flag") == Flag.MISSING.value).sum().alias("missing"),
                pl.col("value_retained").sum().alias("value_retained"),
            )
        )
        station_years.append(part.select("obs_year", "station_name").unique())
        pollutant_years.append(part.select("obs_year", "pollutant").unique())
        generation_years.append(part.select("obs_year", "generation").unique())

    totals = (
        pl.concat(counts)
        .group_by("obs_year")
        .agg(
            pl.col("rows").sum(),
            pl.col("valid").sum(),
            pl.col("invalid").sum(),
            pl.col("missing").sum(),
            pl.col("value_retained").sum(),
        )
    )
    stations = pl.concat(station_years).unique().group_by("obs_year").len(name="stations")
    pollutants = pl.concat(pollutant_years).unique().group_by("obs_year").len(name="pollutants")
    generations = pl.concat(generation_years).unique().group_by("obs_year").len(name="generations")
    return (
        totals.join(stations, on="obs_year")
        .join(pollutants, on="obs_year")
        .join(generations, on="obs_year")
        .with_columns(
            (pl.col("valid") / pl.col("rows")).round(4).alias("valid_ratio"),
            (pl.col("missing") / pl.col("rows")).round(4).alias("missing_ratio"),
        )
        .select(
            "obs_year",
            "rows",
            "stations",
            "pollutants",
            "valid",
            "invalid",
            "missing",
            "value_retained",
            "generations",
            "valid_ratio",
            "missing_ratio",
        )
        .sort("obs_year")
    )


def completeness_matrix(root: Path | None = None) -> pl.DataFrame:
    """Per station × pollutant × year completeness — the heat-map source."""
    parts = [
        part.group_by("obs_year", "station_name", "pollutant").agg(
            pl.len().alias("hours"),
            (pl.col("flag") == Flag.VALID.value).sum().alias("valid"),
        )
        for part in _base(root, columns=["pollutant", "flag"])
    ]
    return (
        pl.concat(parts)
        .group_by("obs_year", "station_name", "pollutant")
        .agg(pl.col("hours").sum(), pl.col("valid").sum())
        .with_columns((pl.col("valid") / pl.col("hours")).round(4).alias("valid_ratio"))
        .sort("obs_year", "station_name", "pollutant")
    )


def station_lifecycle(root: Path | None = None) -> pl.DataFrame:
    """First and last year each station reports — the Gantt-chart source.

    Stations are added, renamed and retired over 40 years. Comparing a national
    mean across years without accounting for this conflates network expansion
    with air-quality change.
    """
    station_year_parts = [
        part.group_by("station_name", "obs_year").agg(pl.len().alias("rows"))
        for part in _base(root, columns=[])
    ]
    return (
        pl.concat(station_year_parts)
        .group_by("station_name")
        .agg(
            pl.col("obs_year").min().alias("first_year"),
            pl.col("obs_year").max().alias("last_year"),
            pl.col("obs_year").n_unique().alias("years_present"),
            pl.col("rows").sum(),
        )
        .with_columns(
            (pl.col("last_year") - pl.col("first_year") + 1).alias("span"),
        )
        .with_columns((pl.col("years_present") < pl.col("span")).alias("has_gap"))
        .sort("first_year", "station_name")
    )


def flag_distribution(root: Path | None = None) -> pl.DataFrame:
    """Flag counts per year — does instrument reliability improve over time?"""
    parts = [
        part.group_by("obs_year", "flag").agg(pl.len().alias("n"))
        for part in _base(root, columns=["flag"])
    ]
    return (
        pl.concat(parts)
        .group_by("obs_year", "flag")
        .agg(pl.col("n").sum())
        .sort("obs_year", "flag")
    )


def retention_asymmetry(root: Path | None = None) -> pl.DataFrame:
    """How often a rejected reading still carries its value, by generation.

    Quantifies the archive-format asymmetry: pre-2018 files keep the number
    behind an invalidation flag, later files discard it. Any long-term trend
    analysis that imputes missing data has to account for this changing
    denominator.
    """
    parts = [
        part.filter(pl.col("flag").is_in(INVALID_FLAGS))
        .group_by("obs_year", "generation")
        .agg(
            pl.len().alias("invalid"),
            pl.col("value_retained").sum().alias("retained"),
        )
        for part in _base(root, columns=["flag", "value_retained", "generation"])
    ]
    return (
        pl.concat(parts)
        .group_by("obs_year", "generation")
        .agg(pl.col("invalid").sum(), pl.col("retained").sum())
        .with_columns((pl.col("retained") / pl.col("invalid")).round(4).alias("retained_ratio"))
        .sort("obs_year")
    )


def sentinel_rates(root: Path | None = None) -> pl.DataFrame:
    """Wind-direction sentinel occurrence per year.

    Settles the Phase 0 question: were 888/999 actually present during
    2010-2017, the window the 2018 project analysed?
    """
    parts = [
        part.filter(pl.col("flag").is_in(list(SENTINEL_FLAGS)))
        .group_by("obs_year", "pollutant", "flag")
        .agg(pl.len().alias("n"))
        for part in _base(root, columns=["pollutant", "flag"])
    ]
    return (
        pl.concat(parts)
        .group_by("obs_year", "pollutant", "flag")
        .agg(pl.col("n").sum())
        .sort("obs_year", "pollutant", "flag")
    )


def wind_direction_totals(root: Path | None = None) -> pl.DataFrame:
    """Denominator for the sentinel rate."""
    parts = [
        part.filter(pl.col("pollutant").is_in(["WD_HR", "WIND_DIREC"]))
        .group_by("obs_year")
        .agg(pl.len().alias("wind_cells"))
        for part in _base(root, columns=["pollutant"])
    ]
    return pl.concat(parts).group_by("obs_year").agg(pl.col("wind_cells").sum()).sort("obs_year")


_NO_PAIRS = pl.DataFrame(schema={"obs_year": pl.Int16, "paired_hours": pl.UInt32})


def pm_pair_diagnostics(root: Path | None = None) -> pl.DataFrame:
    """PM2.5 vs PM10 per year: correlation, ratio, and impossible pairs.

    PM2.5 is a physical subset of PM10, so ``PM2.5 > PM10`` cannot happen — but
    the two are measured by separate instruments with independent errors, and
    it does. Quantifying that rate matters because the 2018 project used PM10
    as a *predictor* of PM2.5 and reported r = 0.887 on monthly means. Computing
    the same correlation hourly shows how much of that came from averaging away
    independent instrument noise rather than from any relationship.
    """
    paired_parts: list[pl.DataFrame] = []
    for part in _base(root, columns=["pollutant", "flag", "value"]):
        paired = part.filter(
            pl.col("pollutant").is_in(["PM2.5", "PM10"])
            & (pl.col("flag") == Flag.VALID.value)
            & pl.col("value").is_not_null()
        ).select("obs_year", "station_name", "ts_local", "pollutant", "value")
        if paired.is_empty():
            continue
        wide = paired.pivot(
            on="pollutant",
            index=["obs_year", "station_name", "ts_local"],
            values="value",
            aggregate_function="first",
        )
        if {"PM2.5", "PM10"} <= set(wide.columns):
            paired_parts.append(wide.select("obs_year", "PM2.5", "PM10"))

    if not paired_parts:
        return _NO_PAIRS.clone()

    wide = pl.concat(paired_parts)
    # A pivot only produces columns for the values it saw. A store holding PM10
    # and no PM2.5 is an ordinary state — PM2.5 monitoring started years later,
    # and any partial or single-pollutant store is in it — but it used to reach
    # ``drop_nulls`` and raise ColumnNotFoundError, which took down every other
    # report in the run with it, because ``build_reports`` computes them in one
    # loop. Nothing to pair is an answer, not a crash.
    if not {"PM2.5", "PM10"} <= set(wide.columns):
        return _NO_PAIRS.clone()

    wide = wide.drop_nulls(["PM2.5", "PM10"])
    if wide.is_empty():
        return _NO_PAIRS.clone()

    # ``PM2.5 / PM10`` is undefined when PM10 reads zero, so the ratio quantiles
    # are computed over the non-zero subset. The *counts* are not, and that used
    # to be the same filter: dropping those rows before counting removed 12,209
    # of the store's 355,209 impossible pairs — 3.4%, and precisely the extreme
    # end, rows reading PM10 = 0 beside PM2.5 = 37. Excluding the worst evidence
    # for a claim in order to state the claim is the failure this whole report
    # exists to avoid, so the exclusion is now counted in ``zero_pm10`` instead.
    return (
        wide.with_columns(
            pl.when(pl.col("PM10") > 0).then(pl.col("PM2.5") / pl.col("PM10")).alias("ratio")
        )
        .group_by("obs_year")
        .agg(
            pl.len().alias("paired_hours"),
            pl.corr("PM2.5", "PM10").alias("hourly_corr"),
            (pl.col("PM2.5") > pl.col("PM10")).sum().alias("impossible"),
            (pl.col("PM2.5") == pl.col("PM10")).sum().alias("identical"),
            (pl.col("PM10") <= 0).sum().alias("zero_pm10"),
            pl.col("ratio").median().alias("ratio_median"),
            pl.col("ratio").quantile(0.05).alias("ratio_p05"),
            pl.col("ratio").quantile(0.95).alias("ratio_p95"),
        )
        .with_columns(
            (pl.col("impossible") / pl.col("paired_hours")).round(5).alias("impossible_rate"),
            (pl.col("identical") / pl.col("paired_hours")).round(5).alias("identical_rate"),
        )
        .sort("obs_year")
    )


PUBLICATION_EVENT_COLUMNS = pl.Schema(
    {
        "event_id": pl.String(),
        "station_name": pl.String(),
        "event_kind": pl.String(),
        "effective_from": pl.String(),
        "source_url": pl.String(),
        "source_published_on": pl.String(),
        "source_statement": pl.String(),
        "pollutant": pl.String(),
        "rows_at_or_after_event": pl.Int64(),
        "numeric_rows_at_or_after_event": pl.Int64(),
        "null_rows_at_or_after_event": pl.Int64(),
        "first_post_event_ts": pl.Datetime("us"),
        "last_post_event_ts": pl.Datetime("us"),
        "published_after_event": pl.Boolean(),
    }
)

_PUBLICATION_EVENT_REQUIRED = frozenset(
    {
        "event_id",
        "station_name",
        "event_kind",
        "effective_from",
        "source_url",
        "source_published_on",
        "source_statement",
    }
)
_TAIPEI_TZ = timezone(timedelta(hours=8))


def _publication_events(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    raw = config if config is not None else load_conf("station_publication_events")
    events = raw.get("events")
    if not isinstance(events, list) or not events:
        raise ConfigError("conf/station_publication_events.yaml defines no `events`")

    parsed: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    for index, record in enumerate(events):
        if not isinstance(record, dict):
            raise ConfigError(f"station publication event {index} must be a mapping")
        missing = _PUBLICATION_EVENT_REQUIRED - set(record)
        if missing:
            raise ConfigError(f"station publication event {index} is missing {sorted(missing)}")

        cleaned: dict[str, Any] = {}
        for field in _PUBLICATION_EVENT_REQUIRED:
            value = record[field]
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ConfigError(f"station publication event {index} has invalid `{field}`")
            cleaned[field] = value

        if cleaned["event_id"] in event_ids:
            raise ConfigError(f"duplicate station publication event id: {cleaned['event_id']}")
        event_ids.add(cleaned["event_id"])

        try:
            effective = datetime.fromisoformat(cleaned["effective_from"])
            datetime.fromisoformat(cleaned["source_published_on"])
        except ValueError as exc:
            raise ConfigError(f"station publication event {index} has an invalid date") from exc
        if effective.utcoffset() is None:
            raise ConfigError(
                f"station publication event {index} effective_from must include a UTC offset"
            )
        cleaned["_effective_local"] = effective.astimezone(_TAIPEI_TZ).replace(tzinfo=None)
        parsed.append(cleaned)
    return parsed


def station_publication_conflicts(
    root: Path | None = None, *, config: dict[str, Any] | None = None
) -> pl.DataFrame:
    """Measure archive rows after reviewed source events without changing them."""
    measured: list[pl.DataFrame] = []
    for event in _publication_events(config):
        after_event = pl.col("ts_local") >= pl.lit(event["_effective_local"])
        partials = [
            part.filter(pl.col("station_name") == event["station_name"])
            .group_by("pollutant")
            .agg(
                after_event.sum().cast(pl.Int64).alias("rows_at_or_after_event"),
                (after_event & pl.col("value").is_not_null())
                .sum()
                .cast(pl.Int64)
                .alias("numeric_rows_at_or_after_event"),
                (after_event & pl.col("value").is_null())
                .sum()
                .cast(pl.Int64)
                .alias("null_rows_at_or_after_event"),
                pl.col("ts_local").filter(after_event).min().alias("first_post_event_ts"),
                pl.col("ts_local").filter(after_event).max().alias("last_post_event_ts"),
            )
            for part in _base(root, columns=["pollutant", "value"])
        ]
        frame = (
            pl.concat(partials)
            .group_by("pollutant")
            .agg(
                pl.col("rows_at_or_after_event").sum(),
                pl.col("numeric_rows_at_or_after_event").sum(),
                pl.col("null_rows_at_or_after_event").sum(),
                pl.col("first_post_event_ts").min(),
                pl.col("last_post_event_ts").max(),
            )
            .with_columns(
                *[
                    pl.lit(event[field]).alias(field)
                    for field in (
                        "event_id",
                        "station_name",
                        "event_kind",
                        "effective_from",
                        "source_url",
                        "source_published_on",
                        "source_statement",
                    )
                ],
                (pl.col("numeric_rows_at_or_after_event") > 0).alias("published_after_event"),
            )
            .select(*PUBLICATION_EVENT_COLUMNS.names())
            .cast(PUBLICATION_EVENT_COLUMNS)
        )
        measured.append(frame)

    if not measured:
        return pl.DataFrame(schema=PUBLICATION_EVENT_COLUMNS)
    return pl.concat(measured, how="vertical").sort("event_id", "pollutant")


REPORTS = {
    "coverage_by_year": coverage_by_year,
    "pm_pair_diagnostics": pm_pair_diagnostics,
    "completeness_matrix": completeness_matrix,
    "station_lifecycle": station_lifecycle,
    "flag_distribution": flag_distribution,
    "retention_asymmetry": retention_asymmetry,
    "sentinel_rates": sentinel_rates,
    "wind_direction_totals": wind_direction_totals,
    "station_publication_conflicts": station_publication_conflicts,
}


def build_reports(root: Path | None = None) -> dict[str, pl.DataFrame]:
    """Run every report and persist it."""
    destination = outputs_dir("qc")
    destination.mkdir(parents=True, exist_ok=True)

    results: dict[str, pl.DataFrame] = {}
    for name, fn in REPORTS.items():
        console.print(f"  computing [bold]{name}[/bold] …")
        frame = fn(root)
        frame.write_parquet(destination / f"{name}.parquet")
        results[name] = frame
    return results


def _md_table(frame: pl.DataFrame, columns: list[str], *, limit: int = 60) -> str:
    subset = frame.select(columns).head(limit)
    header = "| " + " | ".join(columns) + " |"
    divider = "|" + "|".join(["---"] * len(columns)) + "|"
    rows = [
        "| " + " | ".join("" if v is None else str(v) for v in row) + " |"
        for row in subset.iter_rows()
    ]
    body = "\n".join(rows)
    note = (
        f"\n\n_顯示前 {limit} 列，共 {frame.height} 列。完整資料見 `data/outputs/qc/`。_"
        if frame.height > limit
        else ""
    )
    return f"{header}\n{divider}\n{body}{note}"


def write_markdown(results: dict[str, pl.DataFrame]) -> Path:
    """Render the human-readable data-quality report."""
    coverage = results["coverage_by_year"]
    lifecycle = results["station_lifecycle"]
    retention = results["retention_asymmetry"]
    sentinels = results["sentinel_rates"]
    wind_totals = results["wind_direction_totals"]
    publication_conflicts = results["station_publication_conflicts"]

    total_rows = as_int(coverage["rows"].sum(), what="total rows")
    year_min = as_int(coverage["obs_year"].min(), what="first year")
    year_max = as_int(coverage["obs_year"].max(), what="last year")
    overall_valid = as_float(coverage["valid"].sum()) / total_rows if total_rows else 0.0

    sentinel_section = "本期間資料中**未出現**風向哨兵碼。"
    if not sentinels.is_empty():
        joined = (
            sentinels.group_by("obs_year")
            .agg(pl.col("n").sum().alias("sentinels"))
            .join(wind_totals, on="obs_year", how="left")
            .with_columns((pl.col("sentinels") / pl.col("wind_cells")).round(5).alias("rate"))
            .sort("obs_year")
        )
        sentinel_section = _md_table(joined, ["obs_year", "sentinels", "wind_cells", "rate"])

    pm_pairs = results["pm_pair_diagnostics"]
    pm_section = (
        _md_table(
            pm_pairs,
            [
                "obs_year",
                "paired_hours",
                "hourly_corr",
                "impossible_rate",
                "identical_rate",
                "zero_pm10",
                "ratio_median",
            ],
        )
        if not pm_pairs.is_empty()
        else "_無成對資料。_"
    )

    retention_section = (
        _md_table(retention, ["obs_year", "generation", "invalid", "retained", "retained_ratio"])
        if not retention.is_empty()
        else "_無無效值紀錄。_"
    )

    publication_conflict_section = (
        _md_table(
            publication_conflicts,
            [
                "event_id",
                "station_name",
                "pollutant",
                "rows_at_or_after_event",
                "numeric_rows_at_or_after_event",
                "null_rows_at_or_after_event",
                "first_post_event_ts",
                "last_post_event_ts",
                "published_after_event",
            ],
        )
        if not publication_conflicts.is_empty()
        else "_目前沒有可量測的公告後資料。_"
    )

    doc = DOCS_DIR / "data-quality.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        f"""# 資料品質報告

> 由 `uv run twair qc report` 產生。原始表格位於 `data/outputs/qc/*.parquet`。

## 總覽

| 項目 | 值 |
|---|---|
| 涵蓋年份 | {year_min}–{year_max} |
| 總觀測筆數 | {total_rows:,} |
| 測站數（歷來） | {lifecycle.height} |
| 整體有效值比例 | {overall_valid:.2%} |

## 逐年覆蓋

{_md_table(coverage, ["obs_year", "rows", "stations", "pollutants", "valid_ratio", "missing_ratio"])}

## 測站生命週期

監測網從 1990 年代初的十餘站擴張到近 80 站。**跨年度比較「全台平均」時，
測站組成的變動會與空氣品質變化混淆**，這是原專題完全未處理的問題。

{_md_table(lifecycle, ["station_name", "first_year", "last_year", "years_present", "has_gap"], limit=40)}

## 無效值的測值保留率（格式世代差異）

舊格式把旗標附加在數值後（`15#`），無效值仍保留原始測值；
新格式以旗標整格取代（`#`），測值遺失。

這代表**同一套缺漏填補策略在不同年代的效果並不相同**，
任何跨越 2018 年的長期趨勢分析都必須對此做敏感度檢驗。

{retention_section}

## 風向哨兵碼（888 無風 / 999 儀器故障）

{sentinel_section}

## PM2.5 與 PM10 的配對診斷

PM2.5 在定義上是 PM10 的子集，因此 `PM2.5 > PM10` **物理上不可能發生**——
但兩者由不同儀器獨立量測，誤差各自獨立，所以實際上會發生。

這件事之所以重要：2018 年原始畢業專題把 **PM10 當作 PM2.5 的解釋變數**，
並在月平均資料上得到 r = 0.887。同樣的相關係數改用逐時資料計算會低得多——
差額來自月平均把獨立的儀器雜訊平均掉了，而不是來自任何真實關係。

`zero_pm10` 欄是 PM10 讀數為 0 的配對數。比值在這些配對上沒有定義，
所以它們不計入 `ratio_median`，但**仍計入 `impossible`**——
PM10 讀 0、PM2.5 讀 37 是檔案裡最不可能的一組配對，
先把它排除掉再宣稱不可能率，等於丟掉自己最強的證據。

這一欄本身也是個發現：2017 年以前每年只有個位到數十筆，
2018 年起每年逾千筆，與新格式改以旗標整格取代測值的時點一致。

{pm_section}

## 官方公告與年度檔的來源差異

這張表量測官方公告生效後，年度檔是否仍包含該測站的列與非空數值。這是
**來源之間尚未釐清的差異，不是資料有效性的判定**。本專案不刪除、不補值，也不改寫
任何觀測；`published_after_event` 只表示公告生效後仍量到至少一筆非空數值。

{publication_conflict_section}

## 產出檔案

| 檔案 | 內容 |
|---|---|
| `coverage_by_year.parquet` | 逐年筆數、測站數、有效率 |
| `completeness_matrix.parquet` | 測站 × 測項 × 年 的完整度（熱圖來源） |
| `station_lifecycle.parquet` | 各測站起訖年份與資料缺口 |
| `flag_distribution.parquet` | 逐年旗標分布 |
| `retention_asymmetry.parquet` | 無效值測值保留率 |
| `sentinel_rates.parquet` | 風向哨兵碼出現率 |
| `pm_pair_diagnostics.parquet` | PM2.5/PM10 逐時相關、不可能配對率、比值分布 |
| `consistency/*.parquet` | 物理一致性違反率（逐年） |
| `station_publication_conflicts.parquet` | 公告生效後，年度檔仍發布的列、非空值與來源差異 |
""",
        encoding="utf-8",
    )
    return doc


def run_report(root: Path | None = None) -> None:
    """Entry point for ``twair qc report``."""
    results = build_reports(root)
    doc = write_markdown(results)

    coverage = results["coverage_by_year"]
    console.print(
        f"\n[green]{int(coverage['rows'].sum()):,}[/green] rows across "
        f"[green]{coverage.height}[/green] year(s); "
        f"wrote [bold]{doc}[/bold]"
    )
