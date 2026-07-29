# Rules for AI coding agents

> 給人看的說明：這份檔案是給 AI coding agent（GitHub Copilot、Antigravity、
> Claude Code 等）讀的規則。`.github/copilot-instructions.md` 是它的**產生物**——
> 改這一份，然後跑 `uv run python scripts/mirror_agents.py`，絕不手改副本。
> CI 會用 `--check` 驗，所以忘記重生是紅燈而不是兩個 agent 讀到不同規則。
> 人類看的完整規劃在 `PLAN.md` 與 `PROGRESS.md`。

Read this before changing anything. It is short on purpose.

---

## What this project is

44 years (1982–2025) of Taiwanese hourly air-quality data — 341,442,552
observations — parsed into a canonical Parquet store, analysed, and published
as a static website.

**The whole point of the project is that data problems are measured and
reported, not repaired.** Almost every rule below follows from that.

---

## 🚫 THREE RED LINES

Breaking any of these makes the work worse than not doing it. If a task seems
to require breaking one, **STOP and report back instead of proceeding.**

### 1. NEVER fill, interpolate, or drop a missing value

A null in this codebase is a *finding*, not a defect.

```python
# ❌ NEVER do any of these
df.fill_null(0)
df.drop_nulls()  # unless the task explicitly says to
df.interpolate()
df["mean"].fill_null(strategy="forward")
value if value is not None else 0
```

Two different nulls exist and must stay distinguishable:

| `mean` | `n_days` / `n_valid` | Meaning |
|---|---|---|
| `null` | `0` | The station was not measuring. |
| `null` | `> 0` | It was measuring, but coverage fell below threshold, so the average was **deliberately withheld**. |

A chart must draw a **break** in both cases. Never bridge a gap.

If you find code that looks broken because of nulls, that code is probably
correct. Ask.

### 2. NEVER rewrite a comment or docstring to describe what the code does

Comments here explain **why**, usually recording a bug that cost hours.

```python
# ✅ Existing comment — do not touch
# 1992 and 2008 date their rows M/D/YYYY while every other year uses Y/M/D.
# Parsing only Y/M/D returned null for every timestamp, the null filter
# dropped every row, and the build reported success while losing them.

# ❌ Never replace it with this
# Parse the date column.
```

**Do not "improve", shorten, translate, or reformat any existing comment or
docstring** unless the task explicitly says to. If you add new code, write a
comment about *why* it is that way, or write none at all.

### 3. NEVER state a number you did not measure

No claim about the data goes into code, comments, docs or commit messages
unless it came from actually running something. Do not estimate. Do not carry
a number over from a similar project. Do not round a number you did not compute.

If you need a figure and cannot measure it, write `TODO: measure` and say so.

---

## 🔒 Anonymity — non-negotiable

**No person is named anywhere in this repository.** Not in prose, code,
comments, alt text, YAML, commit messages, or JSON.

The project uses a 2018 undergraduate project as a methodological control
group. Refer to it only as「一份 2018 年的大學畢業專題」or "the 2018 method".
Never name its authors or supervisor. Never reproduce its figures.

If you are about to write a person's name, stop.

---

## ✅ Before you say a task is done

Run all three. All three must pass.

```bash
uv run pytest -q
```
Currently **470 passed, 3 deselected**. This number must **not go down**.

> Raise it in the same commit that adds tests. This line said 299 for long
> enough that the real figure reached 470 — a floor 171 tests below reality,
> which would have let someone delete a third of the suite and still pass the
> rule as written. A guardrail nobody updates is a guardrail that quietly stops
> guarding.

**A green suite on this machine is not the same as a green suite.** Most of
`data/` is gitignored, so a test can reach a local artefact and pass here while
failing for everyone else. Reproduce a clean checkout by pointing the data root
at an empty directory:

```bash
TWAIR_DATA_DIR="$TEMP/twair-empty" uv run pytest -q
```

Same count as above, or something depends on data a fresh clone does not have.
`paths.data_root()` reads the variable at call time, so this needs no fixture.

```bash
uv run ruff check .
```
Must print `All checks passed!`

```bash
uv run ruff format .
```

If you touched anything under `web/`:

```bash
cd web && npm run check
```
Must print `0 errors`.

> On Windows, if a command fails because a file is locked, use
> `uv run --no-sync ...` instead.

**If you cannot make all checks pass, say so plainly and show the failing
output. Do not disable a check, add `# type: ignore` to silence an error you
did not understand, or delete a failing test.**

> ⚠️ **A green check obtained by turning the check off is worse than a red
> one**, because it hides the problem from the next person too.
>
> This has already happened once. Mypy reported `twair.viz` as untyped, and 38
> `# type: ignore[import-untyped]` comments were added to make the error go
> away. Each one switched off type-checking for everything imported from that
> module, so `mypy tests` reported success while 219 real errors sat
> unexamined. The actual cause was a missing `src/twair/py.typed` marker — one
> empty file.
>
> **`twair.*` is this repository's own package.** If mypy claims it is
> untyped, something is wrong with the packaging, not with the import. Never
> suppress it. Report it.

**A suppression is a last resort and always needs a reason.** If you write
`# type: ignore[...]`, the same commit must say in prose why the error is not a
real defect. "To make mypy pass" is not a reason.

---

## Scope discipline

- **Only change the files the task names.** If the fix seems to need another
  file, stop and report — do not expand the scope yourself.
- **Do not reformat files you are not otherwise changing.** A diff full of
  whitespace hides the real change.
- **Do not upgrade dependencies** unless that is the task.
- **Do not delete tests.** If a test fails, the code is wrong, not the test —
  unless the task says otherwise.
- **Commit each completed task separately.** Do not batch unrelated work.

---

## Conventions

**Python 3.12, managed by `uv`.** Run things with `uv run <cmd>`, never bare
`python`.

**Polars, not pandas.** Use pandas only where a library demands it
(statsmodels, sklearn). Do not "simplify" Polars code into pandas.

**Config lives in `conf/*.yaml`,** not in constants. Paths, thresholds, station
lists, pollutant ranges — all of them.

> ⚠️ **Quote every key in `conf/*.yaml`.** YAML turns unquoted `NO`, `YES`,
> `ON`, `OFF` into booleans. This silently deleted nitric oxide (`NO`) from the
> range checks once. Write `"NO":`, never `NO:`.

**Tests are specifications.** They are named as claims, not as
`test_function_1`:

```python
def test_a_bearing_of_exactly_360_lands_in_the_north_sector(self) -> None:
def test_a_withheld_mean_arrives_as_null_not_zero(self) -> None:
```

Keep that style. Fixtures mirror real observed data formats, never invented ones.

**Circular quantities need a modulo before binning.** Wind direction is
circular: 359° and 1° are 2° apart, not 358°. Any new binning of degrees does
`% 360` first. This exact bug has already occurred once in this repo.

**Never `group_by` a Hive partition key** (`year`, `month`) from
`scan_parquet(hive_partitioning=True)` — Polars reports per-file totals as
global. Derive the year from `ts_local` instead.

---

## Commit messages

Explain what was **learned**, not just what changed. Plain prose, no bullet
soup.

**Do not add a `Co-Authored-By` trailer.** Every commit is authored by the
repository owner alone; this is the owner's decision about their own project,
not a claim about how the work was done. Earlier commits carried one and were
rewritten.

Use `git commit -F <file>` for multi-line messages (this is Git Bash on
Windows; PowerShell here-strings break, and apostrophes in `-m` truncate).

---

## Where to look

| Question | File |
|---|---|
| What is the current state? | `PROGRESS.md` |
| What is the overall plan? | `PLAN.md` |
| Why is the archive parsing so complicated? | `docs/archive-formats.md` |
| What are the known data-quality properties? | `docs/data-quality.md` |
| What is the licensing position? | `docs/legal.md` |
| How does the website get its data? | `web/README.md` |

---

## When in doubt

Report back rather than guessing. A task returned with "I stopped because X was
ambiguous" is far more useful than a plausible-looking change that quietly
breaks the null semantics.
