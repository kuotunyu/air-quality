"""``twair status`` — the handoff report, measured instead of remembered.

`PROGRESS.md` records what we meant to do and why, which is the part prose is
good at and a program is not. But intent drifts. An analysis gets re-run and
the export does not; a note saying the site is current stays true-*looking*
forever, because nothing about reading a document reveals that it has gone out
of date. Commit ``f6671c1`` shipped an entire module with no documentation
change at all, and nothing complained.

So the handoff is split in two. The prose keeps the reasoning — why monthly
means, why the placebo spread matters, what the 888/999 measurement settled.
This reports what is actually on disk: which stages exist, when each last ran,
and which are older than the thing they derive from.

The dependency chain is one line long:

    store  ->  analysis outputs  ->  web export

A stage is stale when anything upstream of it is newer. That single comparison
is the only inference in this module; everything else is a measurement.

Deliberately *not* here: whether a result is any good. Freshness is a property
of files and can be read off the filesystem. Correctness is a property of the
analysis and belongs in the tests and the prose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from twair.paths import REPO_ROOT, WEB_DIR, outputs_dir, processed_dir

__all__ = [
    "MODULES",
    "Artefact",
    "ExportState",
    "Module",
    "Status",
    "StoreState",
    "collect_status",
    "render",
]


@dataclass(frozen=True, slots=True)
class Module:
    """An analysis module, its output directory, and how to regenerate it.

    ``reproduce`` is the whole point of this table. Six of these are ``twair
    analyze`` subcommands and one is a loose script, and the difference is
    invisible from the output directory alone — which is precisely the thing
    someone returning after two weeks needs to know and cannot guess.
    """

    directory: str
    reproduce: str
    what: str


# Kept in commit order, so the table reads as the project's own history.
# `tests/test_status.py` asserts every `reproduce` names something that exists,
# so a renamed command breaks a test rather than misleading a reader.
MODULES: tuple[Module, ...] = (
    Module("qc", "twair qc report", "data-quality measurement over all 44 years"),
    Module("m1_replication", "twair analyze m1", "replication of the 2018 method"),
    Module("m2_drivers", "python scripts/run_m2.py", "hourly redo, 5.13M rows"),
    Module("m3_pitfalls", "twair analyze m3", "six paired method comparisons"),
    Module("m4_deweather", "twair analyze m4", "meteorological normalisation"),
    Module("m5_causal", "twair analyze m5", "event studies with placebo controls"),
    Module("m7_sources", "twair analyze m7", "CBPF source direction"),
    Module("m9_forecast", "twair analyze m9", "forecasting scored against persistence"),
)


@dataclass(frozen=True, slots=True)
class StoreState:
    exists: bool
    years: tuple[int, ...]
    partitions: int
    rows: int | None
    bytes: int
    modified: datetime | None

    @property
    def span(self) -> str:
        if not self.years:
            return "empty"
        return f"{min(self.years)}-{max(self.years)} ({len(self.years)} years)"


@dataclass(frozen=True, slots=True)
class Artefact:
    module: Module
    exists: bool
    files: int
    bytes: int
    modified: datetime | None


@dataclass(frozen=True, slots=True)
class ExportState:
    exists: bool
    generated_at: datetime | None
    git_sha: str | None
    files: int
    bytes: int


@dataclass(frozen=True, slots=True)
class Status:
    store: StoreState
    artefacts: tuple[Artefact, ...]
    export: ExportState
    undeclared: tuple[str, ...]
    """Output directories with no entry in :data:`MODULES` — a new module that
    nobody wrote down, which is the failure this whole module is about."""

    @property
    def never_run(self) -> tuple[Artefact, ...]:
        return tuple(a for a in self.artefacts if not a.exists)

    @property
    def stale_export(self) -> tuple[Artefact, ...]:
        """Outputs newer than the export, i.e. results the site does not show."""
        if not self.export.exists or self.export.generated_at is None:
            return ()
        return tuple(
            a
            for a in self.artefacts
            if a.modified is not None and a.modified > self.export.generated_at
        )

    def next_steps(self) -> list[str]:
        """Commands the measured state implies, each with the reason attached."""
        steps: list[str] = []
        for artefact in self.never_run:
            steps.append(
                f"{artefact.module.reproduce}  # {artefact.module.directory} never produced"
            )
        if self.stale_export:
            names = ", ".join(a.module.directory for a in self.stale_export)
            steps.append(f"twair export web  # newer than the export: {names}")
        return steps


def _walk(path: Path) -> tuple[int, int, datetime | None]:
    """File count, total bytes and newest mtime beneath a directory.

    Stat calls only — no file is opened. Over the 521-partition store this
    stays well under a second, which is what keeps `status` a thing you run
    without thinking about it.
    """
    files = 0
    total = 0
    newest: float | None = None
    for entry in path.rglob("*"):
        if not entry.is_file():
            continue
        stat = entry.stat()
        files += 1
        total += stat.st_size
        newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
    return files, total, None if newest is None else datetime.fromtimestamp(newest, tz=UTC)


def _store_state(*, count_rows: bool) -> StoreState:
    root = processed_dir("observations")
    if not root.exists():
        return StoreState(False, (), 0, None, 0, None)

    years = tuple(
        int(p.name.removeprefix("year="))
        for p in sorted(root.iterdir())
        if p.is_dir() and p.name.startswith("year=") and p.name.removeprefix("year=").isdigit()
    )
    # Hive layout is year=/month=, so counting the top level would report 44
    # where the store actually holds 521 leaves. Count what a scan opens.
    partitions = len({p.parent for p in root.rglob("*.parquet")})
    _, total, modified = _walk(root)

    rows: int | None = None
    if count_rows:
        import polars as pl

        # Parquet keeps row counts in the footer, so this reads metadata rather
        # than data: 341M rows come back in about half a second.
        rows = int(
            pl.scan_parquet(str(root / "**" / "*.parquet")).select(pl.len()).collect().item()
        )

    return StoreState(True, years, partitions, rows, total, modified)


def _export_state() -> ExportState:
    data_dir = WEB_DIR / "public" / "data"
    manifest = data_dir / "manifest.json"
    if not manifest.exists():
        return ExportState(False, None, None, 0, 0)

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    raw = payload.get("generated_at")
    generated = datetime.fromisoformat(raw) if raw else None
    if generated is not None and generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)

    files, total, _ = _walk(data_dir)
    return ExportState(True, generated, payload.get("git_sha"), files, total)


def collect_status(*, count_rows: bool = True) -> Status:
    """Read the current state of every stage off disk."""
    artefacts = []
    for module in MODULES:
        directory = outputs_dir(module.directory)
        if not directory.exists():
            artefacts.append(Artefact(module, False, 0, 0, None))
            continue
        files, total, modified = _walk(directory)
        artefacts.append(Artefact(module, files > 0, files, total, modified))

    declared = {m.directory for m in MODULES}
    base = outputs_dir()
    found = {p.name for p in base.iterdir() if p.is_dir()} if base.exists() else set()
    # `build` holds ingest logs rather than analysis results; it has no module.
    undeclared = tuple(sorted(found - declared - {"build"}))

    return Status(
        store=_store_state(count_rows=count_rows),
        artefacts=tuple(artefacts),
        export=_export_state(),
        undeclared=undeclared,
    )


def _mb(size: int) -> str:
    return f"{size / 1_048_576:,.0f} MB"


def _when(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    age = datetime.now(tz=UTC) - moment
    days = age.days
    ago = "today" if days == 0 else f"{days}d ago"
    return f"{moment:%Y-%m-%d %H:%M} ({ago})"


def render(status: Status) -> list[str]:
    """Plain lines, so the report is the same in a terminal and in a test."""
    lines: list[str] = []

    store = status.store
    lines.append("STORE")
    if not store.exists:
        lines.append("  absent — run: twair ingest airtw && twair build")
    else:
        rows = f"{store.rows:,} rows" if store.rows is not None else "rows not counted"
        lines.append(f"  {store.span}, {store.partitions} partitions, {rows}, {_mb(store.bytes)}")
        lines.append(f"  last written {_when(store.modified)}")

    lines.append("")
    lines.append("ANALYSIS OUTPUTS")
    width = max(len(m.directory) for m in MODULES)
    for artefact in status.artefacts:
        name = artefact.module.directory.ljust(width)
        if not artefact.exists:
            lines.append(f"  {name}  never run    {artefact.module.reproduce}")
        else:
            lines.append(f"  {name}  {artefact.files:>3} files   {_when(artefact.modified)}")
    for name in status.undeclared:
        lines.append(f"  {name.ljust(width)}  ?? undeclared — add it to status.MODULES")

    lines.append("")
    lines.append("WEB EXPORT")
    export = status.export
    if not export.exists:
        lines.append("  absent — run: twair export web")
    else:
        lines.append(f"  generated {_when(export.generated_at)} from {export.git_sha}")
        lines.append(f"  {export.files} files, {_mb(export.bytes)}")
        for artefact in status.stale_export:
            lines.append(f"  STALE: {artefact.module.directory} is newer than this export")

    steps = status.next_steps()
    lines.append("")
    lines.append("NEXT")
    if not steps:
        lines.append("  nothing the filesystem knows about — see PROGRESS.md for intent")
    for step in steps:
        lines.append(f"  uv run {step}")

    return lines


def declared_reproduce_targets() -> dict[str, bool]:
    """Whether each module's ``reproduce`` string points at something real.

    A script is checked as a path; a ``twair ...`` command is checked against
    the CLI's registered command names. Used by the test that keeps this table
    from rotting into a list of instructions that no longer work.
    """
    from twair.cli import app

    known: set[str] = set()
    for group in app.registered_groups:
        prefix = group.name or ""
        typer_app = group.typer_instance
        if typer_app is None:
            continue
        for command in typer_app.registered_commands:
            known.add(f"twair {prefix} {command.name}".strip())
    for command in app.registered_commands:
        known.add(f"twair {command.name}".strip())

    results: dict[str, bool] = {}
    for module in MODULES:
        target = module.reproduce
        if target.startswith("python "):
            results[target] = (REPO_ROOT / target.removeprefix("python ").strip()).exists()
        else:
            results[target] = target in known
    return results
