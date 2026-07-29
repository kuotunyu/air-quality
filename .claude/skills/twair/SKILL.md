---
name: twair
description: Working rules for the AirLens Taiwan / twair repo — the reanalysis of a 2018 PM2.5 graduation project over 44 years of MOENV hourly data. Load when touching anything in this repo: the ingest pipeline, QC, the Parquet store, analysis modules, forecasting, or the web front end. Covers architecture, hard-won data gotchas, commands, testing conventions, and the per-commit handoff routine that keeps PROGRESS.md and the docs from drifting.
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
viz/      export.py  = the data layers (L0/L1/meta/manifest)
          story.py   = per-chapter payloads, where editorial choices live
web/      Astro site; charts are SVG in frontmatter, no charting library
```

The split between `export.py` and `story.py` is deliberate: the layers carry
data, the story carries *arguments* — which baseline, which threshold, which
comparison. Every such choice is written into the payload beside the numbers it
produced, so a reader sees the assumption without reading the source.

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

Sentinels 888 / 999 are handled by `twair.qc.sentinels`, but measured over all
44 years they exist **only in 1993–2004** (2.6–6.4%), and are completely absent
in 1982–1992 and 2005–2025.

**The 2018 project's 2010–2017 window contains zero of them.** Phase 0 listed
this as a defect of that work; the full data disproved it. Do not reintroduce
the claim. The circular-mean objection is separate and still stands.

### The two ReadMe editions define 888/999 the opposite way round

2017 says 888=calm, 999=fault. The 2001 edition says 888=風向不定, 999=靜風.
Neither was written when the data was collected, so the tie was broken by
measurement: over 1993–2004, hours flagged 999 have a **median wind speed of
exactly 0.00 m/s** (81.3% under 0.5) against 0.43 for 888 and 1.84 for ordinary
bearings. A broken vane does not coincide with a still anemometer.

**999 is calm; 888 is `variable_direction`.** The store was migrated
(`scripts/migrate_sentinel_flags.py`, 311,593 rows). Pinned by
`test_the_shipped_config_uses_the_definition_the_data_supports`.

**`twair repair` could not fix this**, and that generalises: the sentinel pass
nulls the value and keeps only the flag, so the 888/999 that would let repair
reclassify is gone. Any correction to what a *destructive* QC pass means needs
a migration over the flag, not a repair over the value.

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

The models in Phase 2 are **explanatory, not forecasting**. M9 added the lag
features and cleared the bar — see below.

### One baseline always flatters a forecast; use two

M9's full backtest (74 stations, 2015–2025, 4 rolling splits, ~1.78M test rows
per horizon) has skill against persistence **rising** with horizon (+0.175 at
1h to +0.243 at 48h) while skill against climatology **collapses** (+0.839 to
+0.088) and R² falls fourfold (0.859 to 0.217).

All three describe the same model. Persistence degrades faster than the model,
so the model looks better the further out you go; but it is also decaying
toward "the average for this station, this month, this hour", which only the
climatology column shows. **Beating persistence at 48h is not an achievement
when persistence is beaten by the long-run mean.** Useful range ends near 24h.

**And never report a mean over splits without the worst one.** The first
summary line printed "4/4 horizons beat persistence" while `rolling_1` — the
split with the least training data — sat at **−0.111** at six hours. That is
the same failure as an R² hiding a losing model, one level up.
`summarise_scores` carries `skill_worst_split` beside every mean.

### A lag feature and its target shift in opposite directions

`lag_k` at row `t` holds the value at `t − k + 1` (`lag_1` is the most recent
observation available); the target shifts *forward* by the horizon. The
opposition is the entire safety property.

`tests/test_lags.py` uses a series whose value at each hour **is** the hour
index, so a leak shows up as an exact arithmetic fact rather than as a
suspiciously good score. Lags run on a complete hourly index per station
(`complete_hourly_index`), or a three-day outage hands the model pre-outage
data labelled "an hour ago".

Before believing any forecast score, check leakage on **real** data, not the
fixture: no feature equalling the target in >30% of rows, and correlations
ordered as physics predicts (lag1 0.932 > mean3 0.908 > max3 0.896 > lag2 0.883
> PM10_lag1 0.857) — nothing suspiciously near 1.

### The site must be able to tell two nulls apart

Every L0 cell carries `mean` **and** `n_days`. `n_days == 0` means the station
was not measuring; `n_days > 0` with a null mean means the aggregate was
withheld for insufficient coverage. Both render as a break in the line.

Anything new that ships data to the browser needs the same treatment. A chart
that interpolates across a gap is the exact failure this project documents.

### The wraparound bug was in the wraparound module

`WD_HR // 30 * 30` gave a bearing of exactly 360 its own thirteenth sector —
3,341 rows split from the 285,527 at 0 degrees — inside `pitfalls.py`, the
module written to expose that mistake. Fixed with `% 360` before binning.

**Any new binning of a circular quantity takes the modulo first**, and gets a
test with an exact-360 input.

### `.gitignore` patterns without a leading slash match at every level

A bare `data/` also matched `web/public/data/` and silently kept the whole
website export out of git. It is now `/data/`. Check `git check-ignore -v`
after adding any pattern that names a common directory.

### The Space bundle: `uv run twair export space`

Rebuilds `spaces/forecast/` from the store — four LightGBM models, a demo slice,
a climatology table and a manifest. It is **not** published by that command;
pushing anywhere is manual and needs the owner's say-so.

Four things it does deliberately, each of which cost a debugging round:

- **LightGBM's text format, never pickle**, and written with `Path.write_text`
  rather than `save_model`. The C library resolves paths through the platform
  ANSI code page and cannot write to a non-ASCII directory — which is what this
  repository is called. Load with `model_str=`, not `model_file=`.
- **The manifest owns the feature order.** A tree model takes a bare array and
  cannot tell that column 12 became humidity. Both the trainer and the app read
  the order from one place.
- **A declared station that produces no rows is an error, not a smaller
  bundle.** 陽明 reports PM2.5 all year and has no anemometer, so every row died
  on the wind features and the bundle shipped five stations while the README
  described six. `MODEL_PARAMS` lives in `forecast.py` and is imported, for the
  same class of reason.
- **`BundleReport.summary()` avoids square brackets** because it is printed
  through rich, which reads `[...]` as a style tag and silently drops what is
  inside. It swallowed the row counts whole.

### Six hours is this model's weak horizon

Two independent samples agree, which is what makes it worth writing down. The
backtest's four splits at 6h span −0.111 to +0.303 (0.41, the widest of any
horizon; 1h spans 0.07). A separate fit — train 2015–2024, hold out 2025, six
stations — puts 6h skill at **−0.043**, with no station clearly positive, while
1h, 24h and 48h stay solidly positive.

The likely reason: 6 hours falls between two signals. Too far for the current
rise or fall to still carry, too near for the same hour tomorrow to help.

### Chapter components are named by content, never by number

`Chapter3.astro`, `Chapter4.astro`, `Chapter5.astro` was fine until a chapter
was inserted in the middle: the methodology chapter became chapter 6 while its
file was still called `Chapter5`. Now they are `ChapterSources`,
`ChapterDetection`, `ChapterForecast`, `ChapterMethods`, `Explorer`, and the
number lives only in the page's `.eyebrow` — one place to change.

Inserting a chapter still means renumbering the eyebrows, the `sections` list
in `Base.astro`, and any cross-reference in prose. `astro check` will not catch
a wrong number, but a nav entry pointing at a missing `id` shows up as an
anchor that scrolls nowhere — worth checking in the browser after a reorder.

### Inline series labels collide exactly where the finding is

Chapter 5's whole point is that R² and skill converge at 48h, which put two
end-of-line labels 5px apart at an 11.5px font. Convergence is what these
charts are *for*, so the collision is not an edge case. `spreadLabels` pushes
them to a 13px minimum; any new multi-series chart with inline labels needs it.

### Astro's parser reads `<=` in a template expression as a fragment

`{items.filter(x => x.v <= max).map(...)}` fails to compile with a confusing
"Unable to assign attributes when using <> Fragment shorthand" error. Compute
the filtered list in the frontmatter instead.

### Arrow's declared date unit lies

A DuckDB `DATE` comes back as Arrow `Date32<DAY>`, but `Table.toArray()` has
already scaled it to epoch **milliseconds**. Trusting the unit and multiplying
by 86,400,000 again lands the row around 45,000 BC. `duck.ts` reads the scale
from the magnitude instead, because Arrow has changed this between versions.

Float32 is the other one: L1 rounds to two decimals then casts, so 328.59 comes
back as 328.5899963378906. Format to 7 significant digits at display time —
past that is representation noise, not measurement.

### DuckDB-WASM: `eh`, not `coi`, and behind a dynamic import

The threaded (`coi`) build needs cross-origin isolation headers and GitHub
Pages cannot set headers. Use the single-threaded `eh` bundle.

A static `import * as duckdb` puts ~190 KB in the page's initial bundle for
every reader. Dynamic import inside the handler drops the eager chunk to 3 KB.

The explorer **probes** which L1 files are served (one HEAD each) rather than
trusting the build-time listing, because only PM2.5 and PM10 are committed and
the rest exist only locally. It therefore cannot advertise a table that 404s.

### Grid and flex tracks size to min-content unless told otherwise

One chart with `min-width: 480px` inside a single-column grid widened its track
to 490px on a 375px phone and pushed the whole page sideways. `min-width: 0` on
grid/flex children is in `global.css`; keep it there.

Verify with `document.documentElement.scrollWidth - clientWidth === 0` at
375px, in both colour schemes.

### Polars: do not group_by a Hive partition key

Grouping directly on `year`/`month` from `scan_parquet(hive_partitioning=True)`
makes Polars report per-file totals as global. Derive the year from `ts_local`.

### Google Drive rate-limits after ~34 files

Use `--patient` (60/180/420/900s backoff). Drive returns an HTML interstitial
rather than an error status, so downloads are verified by magic bytes.

## Commands

```bash
uv run twair status          # where everything stands on disk; start here
uv run twair doctor          # which credentials are configured
uv run twair probe sources   # re-resolve download links (they rotate)
uv run twair ingest airtw    # download archives; --years 2010:2017 --patient
uv run twair build           # archives -> canonical Parquet
uv run twair aggregate       # daily + monthly with coverage gating
uv run twair stations        # station identity / zone / type
uv run twair qc report       # data-quality measurement -> docs/data-quality.md
uv run twair summary         # row counts per year
uv run twair stations geo    # cached MOENV register; --refresh to re-fetch
uv run twair export web      # L0 + L1 + story payloads -> web/public/data/
uv run twair export space    # HuggingFace Space bundle; does NOT publish
uv run python spaces/forecast/app.py   # run that demo locally (needs the `space` extra)
```

The site itself:

```bash
cd web && npm install && npm run dev    # http://localhost:4321
npm run build && npm run check          # check must be 0 errors
```

CI cannot regenerate the site's data — it has no copy of the store. **Exporting
is a local step followed by a commit.** L0 and the story payloads are
committed; L1 (55 MB) is gitignored; L2 never leaves HuggingFace.

Long runs (`ingest`, `build`) belong in the background — a full build is hours.
`build_year` catches every exception and records it in the summary, so one bad
archive cannot kill an unattended run.

Incremental artefacts must **merge, not replace**. `year_summary.csv` used to
be rewritten wholesale, so rebuilding one year erased the record of the other
forty-three. Anything written per-run needs the same treatment.

## Conventions

- **Python 3.12, uv.** `uv run --no-sync ...` when a background job holds
  `twair.exe` open on Windows.
- **Sync with `uv sync --all-extras --group dev`**, which is what CI does. A
  bare `uv sync` *removes* the optional extras, and the next `pytest` fails
  with eight collection errors that look like broken imports rather than a
  stripped environment. Nothing warns you.
- **Polars** for data, **pandas** only where a library demands it.
- **Config over constants**: paths, URLs, thresholds, station lists all live in
  `conf/*.yaml`.
- **Comments explain why, not what.** Prefer a sentence about the data's
  behaviour over a restatement of the code.
- **Docstrings carry the scientific reasoning**, especially where a choice
  differs from the original project.
- `ruff check --fix && ruff format` before committing.

### mypy is at zero — keep it there

`mypy src tests` reports no issues across 75 files, and CI gates on it along
with `ruff check .`, `ruff format --check .` and `pytest`. This is recent: the
baseline was over a hundred errors, so a new one used to be invisible. It is
now a regression.

Most of those errors were one thing. A Polars aggregate — `.mean()`, `.std()`,
`.sum()`, `.max()` — is typed as a union of every value a cell could hold, and
mypy cannot narrow it. The sanctioned fix is `twair.scalars`: `as_float`,
`as_int`, `opt_float`. They take `Any` and convert for real, and `as_float`
and `as_int` **raise on null** rather than returning a plausible zero.

A helper called on aggregates everywhere should take `Any` itself rather than
force a wrapper at each call — `viz.story._round` does this for its 43 call
sites, delegating to `opt_float`. Never reach for `cast`: it is a claim to the
checker with no runtime force, so if the value really is None the failure just
surfaces somewhere less useful.

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

## Politically charged material — the subject is the method

The events M5 tests — the 2018 Act, the Taichung plant permits, the COVID
alert — are all partisan flashpoints in Taiwan. This project has no position on
any of them, no competence to evaluate policy, and **no findings that would
support such an evaluation anyway**: all three come back not detected, at fewer
stations than chance alone would produce.

So the question asked, in the site copy and in every write-up, is
methodological: *how large a signal can this data and this method detect?*
Measured answer: the placebo spread is 2.5–3.5 µg/m³ against effects of
0.5–1.6. The noise floor sits above the signal.

Rules for any text touching this:

- **The subject is the method, never the government.** "This method cannot
  detect it", not "the policy did not work".
- **"Not detected" is not "zero".** Say so every time it appears.
- The Taichung null means **the policy may never have taken effect** — the
  revocation was overturned and the eight nearest stations did not move.
  Without that sentence a reader converts "did not happen" into "failed".
- No party names, no officials, no wording that assigns blame.
- No policy recommendations. This project does not make them.

The chapter is titled 「事件效應的偵測極限」 — deliberately the same concept
the data already uses one level down, where `ND` marks a reading below the
instrument's detection limit. The instrument has one for concentration; the
method has one for effects, and this chapter measures it. PLAN.md carries the
full drafting note.

## The anonymity rule — settled, and not negotiable

**No person is named anywhere in this project.** Not the 2018 project's
authors, not its supervisor, not in prose, code comments, alt text, commit
messages, or exported JSON. **No figure, screenshot or layout from the original
is reproduced** — every chart in chapter 5 is recomputed here from the raw data.

Only two kinds of material are used, and neither requires naming anyone:

* **method choices** (monthly means, PM10 as a predictor, linearised wind) —
  methodological facts, discussable on their own terms;
* **published numbers** (β=0.4133, t=92.75, r=0.8865, the VIFs) — facts, not
  expression, and independently recomputable here.

Frame the comparison as *a method* against *a better method*, never as a person
against a better person. The recurring phrasing is "一份 2018 年的大學畢業專題"
and "當年的做法" — never "我的專題" in a way that drags a co-author along, and
never anything that would let a reader identify the people involved.

Before any publish step, grep for names. `docs/legal.md` records the reasoning.

## Open decisions (do not decide unilaterally)

1. **Raw-data redistribution licensing.** `docs/legal.md` documents a conflict
   between the open-data terms and the archive ReadMe. Until the owner rules,
   **do not publish a full copy of the raw hourly records** to HuggingFace.

## Delegating to a cheaper agent

Simple, well-specified work goes to GitHub Copilot / Antigravity (Gemini Flash).
The test is **not** difficulty — it is whether verifying the result requires
re-deriving the answer. Type annotations across 300 errors are a fine delegation
(mypy verifies them); a one-line docstring explaining a design choice is not.

`AGENTS.md` and `.github/copilot-instructions.md` carry the rules those agents
see. **They are mirrors — edit both together.** Regenerate the copy with the
header intact:

```bash
python -c "import pathlib; s=pathlib.Path('AGENTS.md').read_text(encoding='utf-8'); h='<!-- MIRROR OF AGENTS.md — edit both together, or neither. -->\n<!-- GitHub Copilot reads this file automatically for repository custom instructions. -->\n\n'; pathlib.Path('.github/copilot-instructions.md').write_text(h+s, encoding='utf-8', newline='\n')"
```

Never delegate: anything touching null semantics, QC flag meaning, statistical
method choice, existing comments and docstrings, or prose that makes a claim.
**Always verify delegated work yourself** — run the checks, read the diff, and
grep for `fill_null`, `drop_nulls` and `interpolate` before accepting it.

## Handoff — the docs ship *with* the commit, not at the end of the session

This used to say "run this before ending a work session" and it failed, for a
reason worth keeping: **sessions do not end, they get interrupted.** A quota
expires, a context window compacts, a background job is still running. Commit
`f6671c1` shipped M9 — a module, a feature builder and 25 tests — with zero doc
changes, because the ritual was scheduled for a moment that never arrived.

So the trigger is the **commit**, not the session. A commit that changes
behaviour and touches no `.md` is the smell.

Each commit that adds or changes a result:

1. `uv run --no-sync pytest -q` and `ruff check src tests` — both clean.
2. Update **`PROGRESS.md`**: what moved, what is running, what is next.
3. Update the affected **`docs/*.md`**. If a measurement contradicts something
   already written, correct it in place — never leave both versions standing.
4. Tick the phase boxes in **`README.md`** / `PLAN.md` if a phase moved.
5. Add any new gotcha to **this skill** — see below for what qualifies.
6. Commit.

### `uv run twair status` — the half that cannot go stale

Prose records *intent* and drifts silently: a note saying the site is current
reads exactly like one that has gone out of date. So the handoff is split.

```bash
uv run twair status
```

reports what is actually on disk — store span and row count, which analysis
modules have produced outputs and when, whether the web export is older than
something it derives from, and the commands that follow from all of that. The
chain it checks is `store → outputs → export`; a stage is stale when anything
upstream is newer.

`status.MODULES` also records **how to regenerate each output**, which is not
guessable — `m2_drivers` comes from `scripts/run_m2.py`, not a subcommand.
`tests/test_status.py` asserts every entry names something that still exists,
so a renamed command breaks a test instead of misleading a reader.

When adding a module, add it to `MODULES` in the same commit. An output
directory with no entry prints as `?? undeclared`.

### What belongs in this skill

Only what would otherwise **cost the next session an hour**: a measurement that
overturned an assumption, a bug whose symptom pointed at the wrong cause, a
format that does not behave as documented. Not a summary of what the code does
— the code is right there, and a paraphrase of it here is one more thing to
keep in sync.

The test of a good handoff: someone returning in two weeks runs `twair status`
to see where things stand and reads `PROGRESS.md` to see why.
