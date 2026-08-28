<!-- GitHub Copilot reads this file automatically for repository custom instructions. -->

# Rules for coding agents

> This is the repository's sole tracked short-rule source.
> CI gate inventory lives in `.github/workflows/ci.yml`; durable reasoning lives
> in `docs/working-rules.md` and the relevant public technical documents.

Read this before changing anything. It is short on purpose.

When returning to the project: read this file, read `docs/working-rules.md`,
then run `uv run twair status` and consult the relevant public technical docs.
Ignored `.superpowers/project-memory/HANDOFF.md` and `PROGRESS.md` may add local
context when present; their absence in a fresh clone is expected.

---

## What this project is

44 years (1982–2025) of Taiwanese hourly air-quality data — 340,371,384
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

## 🔒 Protected identities — non-negotiable

A small number of private individuals are protected identities. Their names must
not appear in tracked paths or content, generated payloads, alt text, comments,
docstrings, configuration, documentation, or commit messages. The digest
inventory in `conf/anonymity.yaml` is the authority on who they are; it holds no
plaintext.

**Do not reintroduce the frame either.** This project no longer describes itself
against any particular earlier study, and prose that reaches for one — naming a
source document, quoting its published figures, citing its page numbers — is a
regression even when no person is named. The methodological findings are stated
as general facts about method choices, because that is what they are, and each
arm of every comparison is fitted here. See `docs/working-rules.md`.

Formal scholarly citations and conventional technical eponyms are allowed
when they make a method, evidence source, limitation, or reproducible parameter
traceable. Keep enough bibliographic context to verify them. Avoid decorative
or nonessential personal attribution.

`scripts/check_repository_anonymity.py` audits protected identities using a
digest-only inventory; it never stores or prints plaintext. It is separate from
`scripts/check_history_identity.py`, which enforces the repository owner's Git
author, committer, and trailer contract. Both audit every commit reachable from
the proposed revision and both run in CI. Do not narrow either one to the
current tree or add a baseline exemption.

---

## ✅ Before you say a task is done

```bash
uv run python scripts/check_like_ci.py
```

**This runs what CI runs, read from `.github/workflows/ci.yml` itself**, so there
is one list of gates in this repository and it cannot fall behind. It skips only
environment setup (installing Python, `uv sync`, `npm ci`) and names what it
skipped. Unlike CI it does not stop at the first failure, because knowing that
three gates are red rather than one changes what you do next.

`--list` shows the steps without running them; `--job test` runs the Python half
alone when you have not touched `web/`.

This section used to enumerate the gates by hand. It listed six while CI ran
seventeen, so a task could pass everything named here and still turn the build
red — which it did, twice, on one afternoon. The individual commands below remain
because their *reasons* are worth reading, not because the list is the interface.

---

```bash
uv run python scripts/check_history_identity.py
```

This checks every commit reachable from `HEAD`: author, committer, and the
commit body. A latest-commit check is not enough.

```bash
uv run python scripts/check_repository_anonymity.py
```

This checks current and historical paths, blobs, generated text surfaces, and
commit bodies against the digest-only protected-identity inventory.

```bash
uv run python scripts/check_test_count.py
```

This collects the complete suite without project `addopts` and requires exact
equality with `conf/project.yaml`. When a commit adds or removes tests, measure
the new total and update that one value in the same commit.

```bash
uv run pytest -q
```

**A green suite on this machine is not the same as a green suite.** Most of
`data/` is gitignored, so a test can reach a local artefact and pass here while
failing for everyone else. Reproduce a clean checkout by pointing the data root
at an empty directory:

```bash
TWAIR_DATA_DIR="$TEMP/twair-empty" uv run pytest -q
```

Same passed/deselected result as the ordinary run, or something depends on
data a fresh clone does not have.
`paths.data_root()` reads the variable at call time, so this needs no fixture.

> ⚠️ **`check_like_ci.py` cannot answer this question and never could.** It runs
> every step CI runs, on a machine that has the data — so 「would these steps
> pass in CI」 and 「would they pass for someone else」 are different questions and
> it only answers the first. A change can be green through all of it and turn CI
> red on push.
>
> This happened on 2026-08-24. Three new tests asserted on figures that
> `src/twair/viz/story.py` derives from `data/outputs/m2_drivers/`, which is
> present here and gitignored there, so the code took its documented
> no-data branch and the assertions failed. The same commit had written that
> branch and a test proving it works. **Run both commands before pushing a
> commit that adds tests**, and build the frame a test needs rather than reading
> one this machine happens to hold.

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

## Internal working artifacts stay local

Design specs, implementation plans, mockups, review reports and agent scratch
belong under `docs/superpowers/` or `.superpowers/`. Both paths are gitignored.
**Never stage or commit anything under either path.** Internal specs, plans,
mockups, reviews and progress diaries never enter Git history. Stable reusable
decisions belong in the relevant public technical documentation; transient
progress may stay in ignored `.superpowers/project-memory/HANDOFF.md` and
`PROGRESS.md` on this machine.

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
| What must I read before changing anything? | `.github/copilot-instructions.md` |
| What are the stable, hard-won rules? | `docs/working-rules.md` |
| What is actually on disk now? | `uv run twair status` |
| What stable decisions are portable? | The relevant public technical docs |
| What local handoff or progress context exists? | `.superpowers/project-memory/HANDOFF.md` and `PROGRESS.md`, when present |
| What identity may appear in history? | `conf/project.yaml` |
| What is the public release boundary? | The phase table in `README.md` / `README.en.md` |
| What design claims does CI verify the site against? | The executable contracts in `scripts/check_palette.py` and `scripts/check_site_quality.mjs`, run by `.github/workflows/ci.yml` |
| Why is the archive parsing so complicated? | `docs/archive-formats.md` |
| What are the known data-quality properties? | `docs/data-quality.md` |
| What is the licensing position? | `docs/legal.md` |
| How does the website get its data? | `web/README.md` |

---

## When in doubt

Report back rather than guessing. A task returned with "I stopped because X was
ambiguous" is far more useful than a plausible-looking change that quietly
breaks the null semantics.
