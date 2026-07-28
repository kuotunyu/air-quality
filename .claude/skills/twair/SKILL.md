---
name: twair
description: Working rules for the AirLens Taiwan / twair repo — the reanalysis of a 2018 PM2.5 graduation project over 44 years of MOENV hourly data. Load when touching anything in this repo: the ingest pipeline, QC, the Parquet store, analysis modules, or the web front end. Covers architecture, hard-won data gotchas, commands, testing conventions, and the end-of-session close-out ritual.
---

# twair — working rules

## What this project is

A reanalysis of a 2018 university graduation project (《台灣地區之 PM2.5 之影響分析》)
using Taiwan MOENV hourly air-quality data from **1982–2025**.

The point is **not** "same analysis, newer tools". It is:

> Take the original's defects one at a time, and show with the same data that
> fixing them changes the conclusions.

Every module should be traceable to a defect in `PLAN.md`'s table (D1–D11).
If a piece of work does not map to one, ask whether it belongs.

Read `PROGRESS.md` first — it is the current state of play.

## The governing principle

**Measure and publish data quality; never silently repair it.**

The original disposed of its data problems in one sentence
(「將有遺漏值之資料以鄰近測站之資料代替」). Here the *rate* of every problem is
itself a result. Concretely:

- Invalid readings are **flagged, not deleted**.
- Out-of-range values keep their value so they stay inspectable.
- Gap filling defaults to `none`; every filled value carries `imputed` and
  `impute_method`.
- Aggregates below coverage threshold return **null**, never a biased mean.
- Unrecognised tokens become `Flag.UNPARSEABLE` and get reported — never
  coerced, never dropped.

If you find yourself writing code that makes a problem disappear, stop.

## Architecture

```
ingest/   download + parse archives      -> long frames
qc/       flags, sentinels, ranges, consistency, reporting
store/    schema, Parquet writer, stations, aggregates, wide view
analysis/ M1-M10 (Phase 2+)
models/   forecasting (Phase 7)
viz/      website export layers
```

Data flow: `raw archives → long observations (Parquet, Hive year/month) →
{daily, monthly, hourly_wide} → analysis → web export`.

**The long table is the source of truth.** It is the only shape that can
record *why* a value is absent. Wide and aggregate tables are derived and
regenerated, never edited.

## Hard-won gotchas — do not relearn these

### Archive formats do not evolve monotonically

Detect dialect from **each file's own header and magic bytes**. Never build a
year→format lookup. Four independent counterexamples:

- 2008 is an ODS island between CSV runs
- hour labelling flips independently of container (1992 vs 1993; 2012 vs 2013)
- 2024 is 7-Zip while its link still says `.zip`
- 1987 ships XLS with no ODS twin

### Hour columns labelled 1–24 start at midnight

Official ReadMe: 「0時：指 0:00-0:59」. Column `1` is **00:00**, not 01:00.
Getting this wrong shifts 1996–2012 by an hour. Pinned by
`test_hour_one_column_lands_at_midnight`.

### 1992 and 2008 date their rows M/D/YYYY

Every other year uses Y/M/D. Parsing only Y/M/D returned null for every
timestamp in those two years, the null filter dropped every row, and the build
reported success while losing them entirely. `DATE_FORMATS` coalesces over both
orders; they cannot be confused (month 2010 and day 1992 are both invalid).

**The general lesson:** `strict=False` parsing plus a downstream null filter is
how a whole year disappears quietly. `read_archive` now raises when members
parse but yield no rows, and `build_year` treats an empty parse as an error.
Any new "skip the bad rows" filter needs a matching "did everything get
skipped?" guard.

### Flag semantics vary *by year*, not by generation

- legacy: `15#` — flag suffixed, value retained
- modern: `#` — flag replaces the value, value lost

But the split is not clean. Measured over all 44 years, retention is ~1.0 for
most legacy years yet **0.000 in 1997 and 2001, 0.340 in 1998, 0.818 in 1995**
— all "legacy" years. The convention changed year to year within a generation.

**Read `value_retained` from the data per year; never infer it from
`generation`.** `data/outputs/qc/retention_asymmetry.parquet` has the numbers.

### YAML eats `NO`

Unquoted `NO`/`YES`/`ON`/`OFF` become booleans (the Norway problem). This
silently removed nitric oxide from range checks. **Quote every key in
`conf/*.yaml`.** `load_conf` now raises on boolean keys.

### Wind direction is circular

Arithmetic mean of 350° and 10° is 180° — due south, when the answer is north.
Use `circular_mean_expr`. Circular pollutants are marked `circular: true` in
`conf/pollutants.yaml`.

Sentinels 888 (calm) / 999 (fault) are handled by `twair.qc.sentinels`, but
measured over all 44 years they exist **only in 1993–2004** (2.6–6.4%), and are
completely absent in 1982–1992 and 2005–2025.

**The 2018 project's 2010–2017 window contains zero of them.** Phase 0 listed
this as a defect of that work; the full data disproved it. Do not reintroduce
the claim. The circular-mean objection is separate and still stands.

### `NR` is a measured zero, not a missing value

About 90% of hourly rainfall cells carry `NR` (無降雨). Excluding them turns
"mean rainfall" into "mean rainfall intensity while raining" — 2.32 mm against
a true 0.23. The M1 replication found this by failing to match the original.

**The conversion is pollutant-specific.** `RAINFALL` and `RAIN_INT` become 0;
`PH_RAIN`, `RAIN_COND` and `RAIN_TEMP` stay null, because the pH of rain that
never fell is undefined and pH 0 is strongly acidic. See `no_rain_is_zero` in
`conf/pollutants.yaml`.

Select rows with `twair.qc.rainfall.usable()`, not `flag == "valid"`.

### Repairing the store

`twair repair` re-applies the value-level QC passes across all partitions in
~40s, versus hours for a rebuild. Use it whenever a QC rule changes; use a
rebuild only when the *parser* changes.

Anything that writes a partition outside `write_observations` must call
`conform_partition` first. A repair pass once wrote `flag` as String into 298
partitions and the whole store stopped scanning — and re-running found nothing
to fix, because the values were already right. `repair` now also rewrites on
dtype drift.

### PM10 is not a predictor of PM2.5

PM2.5 is a physical subset of PM10. `modelling_columns()` excludes it by
default. It remains available for ratio features. Measured cost of the leak:
**32.1% of the leaking model's R²** (0.524 without, 0.772 with).

### Circular encoding matters for linear models, not for trees

Measured, and it contradicted the expectation: LightGBM does *slightly better*
with the raw bearing (R² 0.537) than with sin/cos (0.524), because trees split
repeatedly and can carve 0-360 into as many pieces as they need.

Under OLS — which is what the 2018 project used — sin/cos gives **2.55x** the
R² of the raw bearing (0.0254 to 0.0647). State the distinction; do not claim
the encoding helps everywhere.

### Persistence beats every explanatory model, and that is not a failure

Persistence (this hour = last hour) reaches R² 0.900 against 0.524 for the best
feature set, because it uses PM2.5's own lag while the models use only
concurrent covariates and no PM2.5 history.

The models here are **explanatory, not forecasting**. Any forecasting work
(Phase 7) must add lag features, and persistence is the bar to clear rather
than a peer to compare against.

### Polars: do not group_by a Hive partition key

Grouping directly on `year`/`month` from `scan_parquet(hive_partitioning=True)`
makes Polars report per-file totals as global. Derive the year from `ts_local`.

### Google Drive rate-limits after ~34 files

Use `--patient` (60/180/420/900s backoff). Drive returns an HTML interstitial
rather than an error status, so downloads are verified by magic bytes.

## Commands

```bash
uv run twair doctor          # which credentials are configured
uv run twair probe sources   # re-resolve download links (they rotate)
uv run twair ingest airtw    # download archives; --years 2010:2017 --patient
uv run twair build           # archives -> canonical Parquet
uv run twair aggregate       # daily + monthly with coverage gating
uv run twair stations        # station identity / zone / type
uv run twair qc report       # data-quality measurement -> docs/data-quality.md
uv run twair summary         # row counts per year
```

Long runs (`ingest`, `build`) belong in the background — a full build is hours.
`build_year` catches every exception and records it in the summary, so one bad
archive cannot kill an unattended run.

Incremental artefacts must **merge, not replace**. `year_summary.csv` used to
be rewritten wholesale, so rebuilding one year erased the record of the other
forty-three. Anything written per-run needs the same treatment.

## Conventions

- **Python 3.12, uv.** `uv run --no-sync ...` when a background job holds
  `twair.exe` open on Windows.
- **Polars** for data, **pandas** only where a library demands it.
- **Config over constants**: paths, URLs, thresholds, station lists all live in
  `conf/*.yaml`.
- **Comments explain why, not what.** Prefer a sentence about the data's
  behaviour over a restatement of the code.
- **Docstrings carry the scientific reasoning**, especially where a choice
  differs from the original project.
- `ruff check --fix && ruff format` before committing.

### Testing

- Tests are **specifications**, named as claims:
  `test_the_wraparound_case_the_2018_project_got_wrong`.
- Fixtures mirror real observed formats, not invented ones.
- Where a fast implementation shadows a readable one (`parse_expr` vs
  `parse_token`), a test asserts they agree.
- Ingest tests never touch the network.
- Tests that scan the whole 341M-row store are marked `@pytest.mark.slow` and
  excluded by default (`addopts` carries `-m 'not slow'`). Run them with
  `pytest -m slow`. Never run two full-store scans in one process — that OOMs.

### Commits

Explain what was *learned*, not just what changed — especially when a finding
contradicts an earlier claim. Prior commits corrected the "three generations by
era" story and the wind-sentinel overreach; keep doing that.

End with:
```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Use `git commit -F <file>` — this is Git Bash, so PowerShell here-strings
(`@'...'@`) break, and apostrophes in `-m` truncate the message.

## Credentials

`uv run twair doctor` contacts each provider rather than checking that a
variable is non-empty — a typo and a revoked key look identical otherwise.

**Never let a credential reach a log.** CWA takes its key as a query
parameter and httpx logs full URLs at INFO, which leaked a key to the terminal
once. `verify.py` wraps every check in `_quiet_http()`. Any new code that puts
a secret in a URL needs the same treatment.

Earth Engine is **optional** and deferred: it only affects the satellite half
of Phase 6, and Copernicus Data Space or NASA Earthdata can replace it. Do not
treat its absence as a blocker.

## Open decisions (do not decide unilaterally)

1. **Raw-data redistribution licensing.** `docs/legal.md` documents a conflict
   between the open-data terms and the archive ReadMe. Until the owner rules,
   **do not publish a full copy of the raw hourly records** to HuggingFace.
2. **Publishing the original PDF.** It carries author and advisor names, is
   gitignored, and needs co-author consent before any figure is reproduced.

## Close-out ritual — run this before ending a work session

1. `uv run --no-sync pytest -q` and `ruff check src tests` — both clean.
2. Update **`PROGRESS.md`**: what moved, what is running, what is next, any new
   open question.
3. Update the affected **`docs/*.md`**. If a measurement contradicts something
   already written there, correct it in place — do not leave both versions.
4. Tick the phase checkboxes in **`README.md`** and `PLAN.md` if a phase moved.
5. Update **this skill** if a new gotcha was learned that would otherwise cost
   the next session an hour.
6. Commit.

The test of a good close-out: someone returning in two weeks reads
`PROGRESS.md` and knows what to type next.
