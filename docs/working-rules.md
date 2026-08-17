# twair — working rules

> Read this before touching anything in this repository: the ingest
> pipeline, QC, the Parquet store, the analysis modules, forecasting, or
> the web front end. It covers the architecture, the data gotchas that cost
> real time to find, the commands, the testing conventions, and the
> portable return path that keeps measured state and public docs in sync.
>
> `AGENTS.md` is the short version. This is the long one.
>
> When returning: read `AGENTS.md`, read this file, run `uv run twair status`,
> and consult the relevant public technical docs. Ignored
> `.superpowers/project-memory/HANDOFF.md` and `PROGRESS.md` may add local
> context when present; their absence in a fresh clone is expected.

## What this project is

A reanalysis of Taiwan MOENV hourly air-quality data from **1982–2025**.

The point is **not** "same analysis, newer tools". It is:

> Take one flawed method choice at a time and show, on the same rows, that
> correcting it changes the conclusion.

The flawed arm is fitted here, by `analysis/baseline.py`, so both arms are real.
Every module should be traceable to a row of `PLAN.md`'s D1–D11 table. If a
piece of work does not map to one, ask whether it belongs.

## The governing principle

**Measure and publish data quality; never silently repair it.**

Data problems are often disposed of in one sentence
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
{daily, monthly} → analysis → web export`.

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

**The baseline's 2010–2017 window contains zero of them.** Phase 0 listed
this as a defect of the window; the full data disproved it. Do not reintroduce
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
a true 0.23. The M1 baseline found this by producing an implausible mean.

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

PM2.5 is a physical subset of PM10. Every module that builds a feature
matrix excludes it by default — see `FEATURE_SETS` in
`analysis/drivers.py`. It remains available for ratio features. Measured cost of the leak:
**32.1% of the leaking model's R²** (0.524 without, 0.772 with).

### Circular encoding matters for linear models, not for trees

Measured, and it contradicted the expectation: LightGBM does *slightly better*
with the raw bearing (R² 0.537) than with sin/cos (0.524), because trees split
repeatedly and can carve 0-360 into as many pieces as they need.

Under OLS — which is what the baseline uses — sin/cos gives **2.55x** the
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

### `git worktree remove` de-registers before it deletes, and reports failure

On Windows this repository sits under a non-ASCII path deep enough that a
worktree's `node_modules` and `.venv` exceed `MAX_PATH`. `git worktree remove`
then prints `error: failed to delete ...: Filename too long` — **after** it has
already dropped the worktree from `git worktree list` and `.git/worktrees`.

The failure message is therefore misleading in the most expensive direction. It
reads as "nothing happened", so the obvious response is to leave the directory
alone and try later; but Git has already forgotten it, so it is now an orphan
that no Git command will ever mention again. Three such orphans accumulated this
way and went unnoticed until a disk survey found them holding 1.37 GB.

Removing one needs the read-only bit cleared first, because Git's own object
files are read-only and both `Remove-Item` and `System.IO.Directory.Delete`
fail on them with access denied:

```bash
cmd.exe /c attrib -R "<path>\*.*" /S /D
cmd.exe /c rd /s /q "\\?\<absolute path>"
```

The `\\?\` prefix is what lifts the path limit; `robocopy <empty-dir> <target>
/MIR` followed by an ordinary delete works too.

**After any worktree removal on this machine, run `git worktree list` and then
list `.worktrees/` and compare.** A directory in the second that is missing from
the first is an orphan, and it will not announce itself.

### An unreferenced script here is usually evidence, not dead code

A tidying pass looked for tracked files nothing refers to and found exactly one:
`scripts/check_verdict_meteorology.py`, imported by nothing, called by no
workflow, named in no document. By the usual heuristic it is dead.

It is the opposite. It is the run behind a naming decision — a QC verdict class
was once called `suspected_instrument`, and that name asserted a cause the data
had not been asked about. The script asks it, over 7,193,172 PM2.5
station-hours, and its docstring carries the resulting table. Red line 3 says a
number in a docstring must come from actually running something; this file *is*
that something for those numbers. `scripts/compare_outlier_baselines.py` and
`scripts/solve_categorical.py` are the same kind of object.

So the reference count is meaningless for this directory. These scripts are
deliberately standalone: they are run once, by hand, to settle a question, and
what survives is the measurement in the docstring plus the ability to re-run it.
Deleting one destroys the provenance of a decision that is still in force, and
the loss is silent — every test still passes, because nothing depended on it.

**Before removing any tracked file for being unreferenced, read its docstring.**
If it records a measurement that something else relies on, it is load-bearing at
the level this project actually cares about. The same trap caught `PRODUCT.md`
in the same pass, from the other direction: 52 KB of prose that looks inert and
is re-derived by three CI gates.

### Searching for the label only ever finds the label

Removing an external comparison from this repository was done by grepping for the
words that named it. That found every one of them and reported the tree clean.
Seventeen references survived, because the load-bearing part of a quoted
comparison is not its label — it is the **numbers**. `7,286`, `0.4133`, `92.75`
and `0.8865` carry no marker at all, and no pattern about 專題 was ever going to
match them. One survivor was a full English paragraph; another was a sentence on
the live methods page describing an external study's design.

**A gate was holding one of them in place.** `check_cjk_spacing.py`'s `MUST_KEEP`
asserted that `N = 7,286` appears on `methods/index.html`, so removing the figure
would have failed the build. A probe written to protect a claim outlived the
claim and started enforcing it.

The check that works is **per quantity, not per word**: for each number a
comparison uses, is this repository's own measurement there instead of somebody
else's? Every figure in the methods chapter is now traceable to
`data/outputs/m1_baseline/` — 6,771 station-months over 72 stations from
`panel.parquet`, β 0.4020 and t 86.28 from `ols.parquet`.

The same reasoning applies to any future removal: enumerate the *claims*, then
check each one, rather than grepping for whatever happened to name them.

### Never write a backslash escape into a file through a shell heredoc

`docs/methodology.md` carried two LaTeX formulas whose `\approx` had become a
literal BEL byte (`U+0007`), because `\a` passed through a Bash heredoc into a
Python `str.replace` and was interpreted as an escape on the way. Both formulas
rendered as `pprox`. The two arrived in *different sessions*, months apart, by
exactly the same mechanism — which is what makes it a rule rather than a slip.

It is worse than it looks, because the repair attempt hits the same layer. Two
successive fixes through the shell failed silently: the search string was
mangled identically to the target, so `str.replace` matched nothing and reported
success. The check that finally worked built the characters arithmetically —
`chr(7)` for the control byte, `chr(92)` for the backslash — so no escaping layer
could touch them.

**Write the script to a file with the editor, then run it.** Do not pipe Python
containing backslash escapes through a heredoc, and do not trust a `grep` for the
damage either: `grep -c pprox` matches a *correct* `\approx` too, which is how
the first investigation concluded there was nothing wrong.

`scripts/` has no gate for this. A scan of all 329 tracked files for stray C0
controls (anything under 0x20 that is not tab, newline or carriage return) came
back clean after the repair, and that scan is the cheap way to check again.

### The Space bundle: `uv run twair export space`

Rebuilds `spaces/forecast/` from the store — four LightGBM models, a demo slice,
a climatology table and a manifest. That command does **not** publish anything.

The configured remote target is
<https://huggingface.co/spaces/steven0226/airlens-taiwan-forecast>. Its live
state must be checked at release time, not recorded here. To push an update
after changing the bundle or `app.py`:

```python
from huggingface_hub import HfApi
from twair.config import get_settings

s = get_settings()
api = HfApi(token=s.hf_token)
api.upload_folder(
    repo_id=f"{s.hf_namespace}/airlens-taiwan-forecast",
    repo_type="space",
    folder_path="spaces/forecast",
    ignore_patterns=["__pycache__/*", "*.pyc"],
)
```

`HF_TOKEN` comes from `.env`, verified without logging by `uv run twair doctor`.
Still needs the owner's go-ahead — this repo publishing to a repo they own is
not standing authorisation for future pushes without asking.

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

### Health numbers: fractions, never counts

`analysis/health.py` reports the **attributable fraction**, which needs a
concentration and a response function. A death count additionally needs a
population and a baseline mortality rate, and this repository holds station
observations, not people — that term would dominate the error while reading as
the solid part of the sentence. Same argument rules out GEMM: it is cause- and
age-specific and there is no age-stratified population to apply it to.

Response functions live in `conf/health.yaml` and **must carry a resolvable
`source_url`**, the same gate `conf/events.yaml` has.

The measured finding is counter-intuitive and worth keeping: the counterfactual
choice takes a *larger* share of the answer as air improves — 17% of the
estimate in 2006, 45% in 2025 — because the excess over the counterfactual is
shrinking toward the counterfactual. Cleaner air makes a health-burden number
more assumption-dependent, not less.

### One panel rule, in `twair/panels.py`

`balanced_panel_options` / `choose_balanced_start` were in `viz/story.py` and
are now shared, because M10 needed the same thing and a second copy would
drift. Anything comparing across years must use them: PM2.5 coverage goes from
5 stations in 1998 to 77 in 2025, so an unbalanced series partly measures the
network. Both callers independently land on 2006 / 68 stations.

`web/src/lib/chart.ts` has `spreadLabels` for the same reason — chapters 5 and
6 both draw converging lines, and converging lines collide their end labels.

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

Chapter 6's whole point is that R² and skill converge at 48h, which put two
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

### Gap filling is opt-in, marked, and never near the store

`qc/gapfill.py` implements the neighbour method beside
three alternatives so it can be priced, not so it can be used. `none` is the
shipped strategy. Every filled column gains `<column>_imputed`, and `_mark`
derives that flag by comparing against the pre-fill frame rather than trusting
each strategy to report honestly — which also catches a strategy overwriting a
value it should have left alone.

Measured over 2010–2017 (`twair analyze m11`): the neighbour method reconstructs
hidden readings at **7.41 µg/m³ MAE** against interpolation's **2.88**, and on
the *same* one-hour gaps it is 7.31 against 2.59 — 2.8x worse on the easiest
case there is. Borrowing from 30 km away loses to a straight line between the
two adjacent hours.

**Its error barely grows with gap length** (7.07 at 2-3h to 8.96 at >48h, over a
20x change). That reads as robustness and is the opposite: the method cannot
tell how hard the problem is, so it fills a three-week outage as confidently as
a one-hour one and says nothing about which is which.

### The site's register is a research write-up, not a feature article

Display copy drifted into news-headline shape: a short claim, a line break, a
reversal. 「台灣的空氣變好了。但沒有一個地方達標。」 reads as a headline, and the
owner asked for the opposite. The rule that fixed it, and that new copy should
follow:

**A heading names the quantity under analysis, not a conclusion with a twist.**
「這二十年到底變好了嗎」 became 「長期趨勢與氣象校正」; 「壞空氣是從哪個方向來的」
became 「污染來向與風速條件」; 「自己查」 became 「資料查詢」. Chapter 5's title was
already right — 「事件效應的偵測極限」 names what is measured.

Avoid in display copy: 到底 / 其實 / 故事 / 值得說 / 戲劇性, rhetorical questions,
and the short-sentence-then-reversal. The body prose was already fine — it
states caveats and numbers, which is the register wanted. This was a headline
problem, not a writing problem.

### Payload prose is printed verbatim — it is not Markdown

Three payload strings in `viz/story.py` carried `**emphasis**` and the site
showed the reader two asterisks, because nothing renders it. Emphasis belongs
in the component, which has real markup. Pinned by
`test_no_exported_string_contains_markdown_emphasis`, which walks every
exported payload rather than naming the three that were wrong.

### A payload nobody imports is a finding nobody sees

`deweather.json` was exported on every run from Phase 4 onward and read by no
component, while its docstring said "Chapter 1's second line" from the first
commit. Chapter 5 meanwhile pointed readers at "第一章那條氣象正規化後的月序列"
— a chart that did not exist. Neither `astro check` nor any test catches that:
an unused export is valid, and a cross-reference in prose is just prose.

**When adding a payload, wire it into a component in the same commit**, or it
becomes a finding that exists only in `data/outputs/`.

### The two deweathered lines must come from the same rows

Chapter 1's existing trend is a 68-station balanced panel over the daily
aggregates; M4 fitted 74 stations. Plotting M4's normalised series against that
line would put the station-set difference inside the gap between them — which
is precisely what chapter 1's *first* correction exists to remove.

So `story._deweather_series` re-aggregates **both** lines from M4's own
`monthly.parquet`, on a panel balanced by the same rule chapter 1 uses
(maximise station-years; a station-year needs >= 11 months). Measured: 61
stations, 2008-2025.

**Annual only.** The normalised monthly series is nearly flat within a year by
construction — `doy` and `hour` are resampled with the meteorology, so the
seasonal cycle goes too. Plotted monthly it looks like a broken chart.

The result is worth knowing: observed falls 20.36 ug/m3 against the normalised
11.52, so **43.4% of the fall is weather** — and M4's own median-of-per-station
slope ratios says 42.2%. Two unrelated aggregations of the same fits landing
1.2 points apart is worth more than either number alone, so both ship.

### Chapter 8 has seven pitfalls, and the seventh comes from a different module

Pitfalls 01-06 read `story/pitfalls.json` (M3); 07 reads `story/imputation.json`
(M11). Adding one meant a second payload rather than extending the first,
because the two analyses have different shapes and forcing them into one table
would have meant reshaping the M3 evidence to fit.

Its chart relies on `linePath` breaking at nulls: interpolation's series is null
past its 3-hour limit, so the line simply stops, and the break *is* the finding
rather than a rendering gap.

**The gap-length bucket order ships in the payload.** Sorted as strings, ">48h"
comes first and "13-48h" lands between "1h" and "2-3h" — which destroys the only
thing the chart is for. Pinned by `test_the_gap_buckets_ship_in_physical_order`.

### A fill rate without the gap-length distribution says nothing

72.5% of gaps are one hour, but those are only 20.7% of missing hours; 0.8% of
gaps are longer than 48h and they are **43.4%** of missing hours. "How much is
missing" has two answers pointing opposite ways, and the strategies are each
best at one end. Always report the distribution beside the rate.

The same reasoning governs the evaluation: hiding isolated cells would hand
interpolation a problem it cannot fail. `mask_like_reality` draws run lengths
from the observed distribution, and scores are reported per bucket — a pooled
number hides the effect being measured.

### Downstream R² cannot compare imputation strategies

Filling only changes rows the baseline could not use, so each strategy ends up
scored on a different test set. The "more usable rows" explanation does not
rescue it either: the baseline already uses 95.2% of station-hours and the
neighbour strategy 95.9%, yet the neighbour strategy gains the most R².

A well-posed version fixes the evaluation set across strategies while letting
the training set vary, which `run_drivers` does not support. The number was
computed, found confounded, and **removed** — see `analysis/imputation.py`.

### The suite passed for months in an environment nobody else has

The first CI run this repository ever had — 2026-07-29, the day it got a remote
— failed. One test, `test_a_closed_station_gets_the_card_for_its_last_good_year`,
reached `story._stations()`, which reads `data/outputs/qc/stations.parquet`.
That file is produced by `twair stations` and is gitignored, so it exists here
and nowhere else. The test next to it already stubbed `_stations` for the same
reason; this one just never had to.

Nothing was wrong with the test suite as a description of the code. What was
wrong is that "green" had only ever been measured on a machine holding 1.5 GB
of gitignored artefacts. **Before pushing, reproduce a clean checkout:**

```bash
TWAIR_DATA_DIR="$TEMP/twair-empty" uv run pytest -q
```

`paths.data_root()` reads the variable at call time, so pointing it at an empty
directory makes every local artefact look absent — which is exactly what CI
sees. Same count as the ordinary run, or something is reaching data a fresh
clone does not have.

The general shape is worth keeping: a check that has never run in the
environment it is supposed to protect has not been passing, it has been
untested.

### Development is Windows and CI is Linux, so type-check both

Three CI failures have now been Linux-only while every local gate was green: the
ANSI styling that split a CLI option across raw-string spans, a Chrome
debugging-port timeout, and `subprocess.CREATE_NEW_PROCESS_GROUP`, which exists
only in typeshed's Windows stub. The last one is the instructive one, because the
runtime code was already correct:

```python
subprocess.Popen(
    argv,
    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
)
```

That branches correctly at run time and is *unverifiable* locally. mypy narrows
`sys.platform` in an `if` statement but evaluated both arms of this conditional
expression, so it passed against the Windows stub here and failed against the
POSIX stub in CI. The fix is the statement form, assigned once at module level.

**mypy can be told which platform to assume, so this is catchable before pushing:**

```bash
uv run mypy --platform linux src tests
uv run mypy --platform linux scripts
```

Run against the broken code that reproduces CI's exact error; run against the fix
and it passes. Add it whenever a change touches `subprocess`, `os`, `signal`,
`pathlib` internals or anything else whose stubs are platform-conditional — it
costs one extra mypy pass and is the only way to see a POSIX-only error from
here. The same reasoning as the empty-data run above: an environment CI has and
you do not is an environment your gates have never actually tested.

### A credential in a URL leaks by two routes, not one

`net.quiet_http` was written after httpx logged a real CWA key at INFO. Its
docstring asks the next module that needs it to use it; `freshness.py` was that
module and rediscovered the problem instead — the key reached the terminal on
its first real run.

The second route is the one that is easy to miss. `httpx.HTTPStatusError`
carries the full request URL **in its own message**, so

```python
except httpx.HTTPError as exc:
    log.warning("failed: %s", exc)     # ❌ leaks past the logger you silenced
```

writes the key even inside `quiet_http()`. Log the status code, or the
exception *type*, never the exception. Both routes are pinned by
`TestTheKeyNeverReachesTheLog`.

**Any new call that puts a secret in a query parameter needs both halves.**

### Month arithmetic wants a zero-based index

`freshness.expected_year` computed `year * 12 + month`, which counts December
of year Y as "12 months elapsed" when it is the month the year ends *in*. Every
threshold arrived a month early. Use `(month - 1)`.

The parametrised case that catches it (2026-06-30) was in the test file from
the start and was failing when the work arrived — worth remembering that a WIP
can look finished and still be red. It also had 4 mypy errors.

### Staleness is a property of the calendar, not of the diff

`twair freshness` is deliberately **not** a step in `ci.yml`. The same commit is
fresh in June and stale in July, so in the PR gate it would eventually turn
every pull request red for a condition no pull request can fix — clearing it
means hours of local ingest against 341M rows. The cheapest way to merge would
become deleting the check, which is the failure `AGENTS.md` names.

It lives in `.github/workflows/freshness.yml`, weekly. Not monthly: GitHub
disables scheduled workflows after 60 days of repository inactivity, and this
repo *is* inactive between refreshes, so a green run in the last seven days is
the only cheap evidence the check is still switched on.

Two things there are load-bearing and look like noise: `shell: bash` is what
turns on `pipefail` (without it the `tee` swallows twair's exit code and the job
is green forever), and `uv run --no-dev` must match the `uv sync --no-dev` above
it (a bare `uv run` re-syncs the default groups and reinstalls pytest and mypy).

The verdict is **fully offline** — `missing_years` is `meta.json` plus the clock
— so a missing `MOENV_API_KEY` or an MOENV outage cannot change pass/fail. That
is what makes a cron job safe to fail on. Never gate the job on the secret.

### "Cannot answer" is a third state, and it is not "fine"

`FreshnessReport.is_unknown` is separate from `is_stale` because
`export._data_through()` catches a bare `Exception` and returns `None`: an
export can lose the field without anything failing. Reported as fresh, the
weekly job would stay green forever while measuring nothing. Both states fail
`--fail-if-stale`; only `is_stale` names a year to go and fetch.

Same shape as the two nulls in the aggregates. When absence can happen for two
different reasons, the two reasons have to stay distinguishable.

## Commands

```bash
uv run twair status          # where everything stands on disk; start here
uv run twair freshness       # has upstream published a year we have not ingested?
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
uv run twair export space    # HuggingFace Space bundle; rebuild only, does not push
uv run python spaces/forecast/app.py   # run that demo locally (needs the `space` extra)
```

The site itself:

```bash
cd web && npm install && npm run dev    # http://localhost:4321
npm run build && npm run check          # check must be 0 errors
```

CI cannot regenerate the site's data — it has no copy of the store. **Exporting
is a local step followed by a commit.** The publication policy for each layer
lives in `web/README.md`; L2 is not published at all.

Long runs (`ingest`, `build`) belong in the background — a full build is hours.
`build_year` catches every exception and records it in the summary, so one bad
archive cannot kill an unattended run.

Incremental artefacts must **merge, not replace**. `year_summary.csv` used to
be rewritten wholesale, so rebuilding one year erased the record of the other
forty-three. Anything written per-run needs the same treatment.

### A wheel runs in a workspace, not in its installation directory

In a source checkout, the repository root is the workspace. An installed wheel
uses the current working directory unless `TWAIR_WORKSPACE_DIR` is set in the
process environment before startup. Relative `TWAIR_DATA_DIR`, `.env`,
`conf/`, `docs/`, `reports/`, and `web/` paths are rooted there.

The wheel carries a reviewed, read-only snapshot of every tracked
`conf/*.yaml`. `load_conf()` first looks for a workspace override and otherwise
uses that packaged snapshot. `write_conf()` always writes the workspace
override; a probe or register refresh must never mutate `site-packages`.
`scripts/check_python_package.py` builds a direct wheel and sdist, rebuilds a
wheel from the sdist, and exercises both outside the checkout. Run it before a
Python package release; `twair --help` is not a sufficient package test.

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

`mypy src tests` and `mypy scripts` must both report no issues. They run as two
commands because `scripts/` contains standalone modules rather than a package;
combining the paths makes mypy resolve the same file under two module names.
CI gates on both commands along with ruff and pytest.

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

The complete inventory is an equality gate, not a minimum:

```bash
uv run python scripts/check_test_count.py
```

It clears project `addopts` while collecting, so deselected slow tests remain
part of the reviewed total. Both a lower and a higher count fail: accepting a
higher count without updating the record lets the supposed floor drift below
the suite again. Add or remove tests and update the measured value in
`conf/project.yaml` in the same commit.

### Commits

Explain what was *learned*, not just what changed — especially when a finding
contradicts an earlier claim. Prior commits corrected the "three generations by
era" story and the wind-sentinel overreach; keep doing that.

**No `Co-Authored-By` trailer, ever.** The owner wants this repository authored
by one person and its contributor list to name one person, which is their call
to make about their own project.

This has gone wrong twice, and the second time is the instructive one. Checking
`git log --format='%an <%ae>' | sort -u` and seeing a single name proves
nothing: GitHub builds its Contributors panel from co-author trailers as well as
the author field, so that check passed while the panel showed two. The first 55
commits were rewritten before the repo had a remote — free then. Forty-nine
more were rewritten after it had one, which cost a force-push and a diverged
history.

The completion and CI gate is:

```bash
uv run python scripts/check_history_identity.py
```

It checks every commit reachable from `HEAD`, not just the latest one, and
names each offending SHA and field without copying an unexpected identity into
a public log. Manual `git log` commands are diagnostic aids, not the gate.
If you are about to add a trailer, do not.

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

## Protected identities — settled, and not negotiable

A small number of private individuals are protected identities. Their names
do not belong in tracked paths or content, prose, code comments, alt text,
commit messages, configuration, or exported JSON. No figure, screenshot or
layout from any third-party document is reproduced — every chart here is
computed from the raw data by this project.

This is not a ban on scientific attribution. Formal citations and conventional
technical eponyms remain when they identify a method, evidence source,
limitation, or reproducible parameter, with enough bibliographic context to
verify them. Decorative or nonessential personal attribution does not.

**The frame goes too, not only the names.** Since 2026-08-17 this project does
not describe itself against any particular earlier study. The comparison is
between method choices, and *both arms are fitted here* — the flawed one by
`analysis/baseline.py`, on the same rows as the corrected one. That is what
licenses the comparison, and it needs no external document.

So prose that reaches for one is a regression even when nobody is named:
identifying a source study, quoting its published coefficients, citing its page
numbers, or wording like 「原專題」/「當年的做法」. Say what the choice is and
what it costs. Every cost in D1–D11 was measured in this repository, which is
why none of them needs a citation.

Before any publish step, run `uv run python scripts/check_repository_anonymity.py`.
The checker reads only normalized digests and redacts every match. It is
deliberately separate from `scripts/check_history_identity.py`, which proves the
Git author/committer/trailer contract. The 2026-08-03 production rewrite cleared
the measured reachable-history blocker after an explicit owner decision; both
checks now run in CI against the proposed head. Do not narrow either check or
add a baseline exemption. `docs/legal.md` records the underlying publication
reasoning.

## Settled decisions — do not reopen these

1. **Raw-data redistribution: L2 is not published, anywhere.** `docs/legal.md`
   documents a conflict between the open-data terms and the archive ReadMe.
   The 2026-07-28 decision **sidesteps it rather than resolving it**: ship the
   derived layers (L0, L1) and the pipeline that rebuilds everything else, so
   the data stays effectively available without redistributing the raw hourly
   record. No enquiry was sent to MOENV and none is planned — this is a side
   project, not a submission, and it does not need a written basis.

   So L2 is not "pending an answer" and not "on HuggingFace". It does not
   exist outside this machine. `twair export web --levels L2` refuses for that
   reason.

## Delegation boundary

Simple, well-specified work may be delegated. The test is **not** difficulty —
it is whether verifying the result requires re-deriving the answer. Mechanical
type corrections are suitable when mypy verifies them; prose explaining a
scientific design choice is not.

`AGENTS.md` is the source for the two tracked short-rule entries; never edit
the generated copy. Regenerate it:

```bash
uv run python scripts/mirror_agents.py
```

CI runs the same script with `--check`, so a forgotten regeneration is a red
build rather than two entry points reading different rules.

The old one-liner wrote the copy with `newline="\n"` while `AGENTS.md` is
checked out CRLF, so `diff` reported all 224 lines as different *immediately
after regenerating* — the contents were identical the whole time. That is worse
than no check: a comparison that always fails teaches you to stop running it,
and then real drift looks exactly like the noise you have been ignoring. The
script copies the source's bytes verbatim so byte equality is the right test.

Never delegate: anything touching null semantics, QC flag meaning, statistical
method choice, existing comments and docstrings, or prose that makes a claim.
**Always verify delegated work yourself** — run the checks, read the diff, and
grep for `fill_null`, `drop_nulls` and `interpolate` before accepting it.

## Internal agent work stays local

Design specs, implementation plans, mockups, reviews, progress diaries and
other agent working artefacts are useful for resuming work on this machine,
but they are not public project documentation and never enter Git history.
Keep them under gitignored `docs/superpowers/` or `.superpowers/` and never
stage either path.

Stable reusable decisions and measured evidence belong in the relevant public
technical documentation. Transient progress may stay in ignored
`.superpowers/project-memory/HANDOFF.md` and `PROGRESS.md`; these files are
optional local context, not part of the portable return path. This keeps GitHub
focused on the project rather than the private mechanics used to develop it.

## Return path — public facts ship with the commit

`uv run twair status` and the public technical documentation are the portable
return path because both survive a fresh clone. A commit that changes a durable
result or public behaviour and leaves the relevant public docs stale is the
smell.

Each commit that adds or changes a result:

1. Run every completion gate in `AGENTS.md`; partial test or lint paths are not
   substitutes for the repository gates.
2. Update the affected **`docs/*.md`**. If a measurement contradicts something
   already written, correct it in place — never leave both versions standing.
3. Tick the phase boxes in **`README.md`** / `PLAN.md` if a phase moved.
4. Add any new reusable gotcha to **this document** — see below for what
   qualifies.
5. Keep transient progress in ignored `.superpowers/project-memory/HANDOFF.md`
   or `PROGRESS.md` when local continuity needs it; never stage either file.
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

Since the freshness work it also prints an UPSTREAM block — one link further
out than the chain, answering "has MOENV published a year we have not
ingested". **Only the offline half is wired in**: it builds a `FreshnessReport`
from `read_data_through` alone and never calls `check_freshness`, which has a
30-second timeout. A `status` that can hang for half a minute stops being the
command you run without thinking, so the obvious simplification is the wrong
one, and a test replaces `httpx.get` with a raiser to keep it that way.

`status.MODULES` also records **how to regenerate each output**, which is not
guessable — `m2_drivers` comes from `scripts/run_m2.py`, not a subcommand.
`tests/test_status.py` asserts every entry names something that still exists,
so a renamed command breaks a test instead of misleading a reader.

When adding a module, add it to `MODULES` in the same commit. An output
directory with no entry prints as `?? undeclared`.

### What belongs in these working rules

Only what would otherwise **cost the next session an hour**: a measurement that
overturned an assumption, a bug whose symptom pointed at the wrong cause, a
format that does not behave as documented. Not a summary of what the code does
— the code is right there, and a paraphrase of it here is one more thing to
keep in sync.

The test of a good return path: someone returning in a fresh clone can run
`twair status` and read the relevant public technical docs to recover the
portable state and stable decisions. On the working machine, ignored
`.superpowers/project-memory/HANDOFF.md` and `PROGRESS.md` may add transient
context, but the published project never depends on their presence.
