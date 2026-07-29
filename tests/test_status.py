"""Tests for the measured half of the handoff.

`twair status` exists because prose goes stale invisibly. That argument only
holds if the status report does not go stale invisibly itself — a table of
"how to regenerate this" that names a command which no longer exists is worse
than no table, because it looks authoritative.

So the contract tests here are the point of the file. The rendering tests just
keep the output readable when a stage is missing, which is the state someone
cloning the repo sees first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from twair.status import (
    MODULES,
    Artefact,
    ExportState,
    Module,
    Status,
    StoreState,
    collect_status,
    declared_reproduce_targets,
    render,
)


def _artefact(name: str, *, modified: datetime | None, exists: bool = True) -> Artefact:
    return Artefact(Module(name, f"twair analyze {name}", name), exists, 3, 1024, modified)


def _status(*artefacts: Artefact, export_at: datetime | None) -> Status:
    return Status(
        store=StoreState(True, tuple(range(1982, 2026)), 521, 341_442_552, 605_000_000, None),
        artefacts=artefacts,
        export=ExportState(export_at is not None, export_at, "abc1234", 84, 61_000_000),
        undeclared=(),
    )


class TestTheReproduceTableCannotRot:
    def test_every_module_names_something_that_exists(self) -> None:
        """A renamed CLI command must break a test, not mislead a reader.

        `m2_drivers` is the one that makes this worth checking: it is produced
        by a loose script rather than a subcommand, which is invisible from the
        output directory and impossible to guess two weeks later.
        """
        missing = [target for target, found in declared_reproduce_targets().items() if not found]

        assert not missing, f"declared but not found: {missing}"

    def test_module_directories_are_unique(self) -> None:
        names = [m.directory for m in MODULES]

        assert len(names) == len(set(names))

    def test_an_undeclared_output_directory_is_reported_rather_than_ignored(self) -> None:
        """A new module that nobody wrote down is the failure being guarded."""
        status = Status(
            store=StoreState(False, (), 0, None, 0, None),
            artefacts=(),
            export=ExportState(False, None, None, 0, 0),
            undeclared=("m11_mystery",),
        )

        assert any("undeclared" in line for line in render(status))


class TestStaleness:
    def test_an_output_newer_than_the_export_is_stale(self) -> None:
        exported = datetime(2026, 7, 28, tzinfo=UTC)
        status = _status(
            _artefact("m9_forecast", modified=exported + timedelta(days=1)), export_at=exported
        )

        assert [a.module.directory for a in status.stale_export] == ["m9_forecast"]

    def test_an_output_older_than_the_export_is_not(self) -> None:
        exported = datetime(2026, 7, 28, tzinfo=UTC)
        status = _status(
            _artefact("m4_deweather", modified=exported - timedelta(days=3)), export_at=exported
        )

        assert status.stale_export == ()

    def test_a_missing_export_does_not_claim_everything_is_stale(self) -> None:
        """Nothing is *stale* relative to an export that was never made."""
        status = _status(
            _artefact("m9_forecast", modified=datetime(2026, 7, 29, tzinfo=UTC)), export_at=None
        )

        assert status.stale_export == ()

    def test_a_module_that_never_ran_is_listed_separately_from_a_stale_one(self) -> None:
        status = _status(
            _artefact("m9_forecast", modified=None, exists=False),
            export_at=datetime(2026, 7, 28, tzinfo=UTC),
        )

        assert [a.module.directory for a in status.never_run] == ["m9_forecast"]
        assert status.stale_export == ()


class TestNextSteps:
    def test_a_never_run_module_produces_its_own_command(self) -> None:
        status = _status(
            _artefact("m9_forecast", modified=None, exists=False),
            export_at=datetime(2026, 7, 28, tzinfo=UTC),
        )

        assert any("twair analyze m9_forecast" in step for step in status.next_steps())

    def test_a_stale_export_produces_an_export_command_naming_the_cause(self) -> None:
        exported = datetime(2026, 7, 28, tzinfo=UTC)
        status = _status(
            _artefact("m7_sources", modified=exported + timedelta(hours=2)), export_at=exported
        )

        steps = status.next_steps()
        assert any("twair export web" in s and "m7_sources" in s for s in steps)

    def test_a_current_project_asks_for_nothing(self) -> None:
        exported = datetime(2026, 7, 28, tzinfo=UTC)
        status = _status(
            _artefact("m4_deweather", modified=exported - timedelta(days=1)), export_at=exported
        )

        assert status.next_steps() == []


class TestRendering:
    def test_an_empty_checkout_says_what_to_run_first(self) -> None:
        """The first thing a reader with no data sees must be actionable."""
        status = Status(
            store=StoreState(False, (), 0, None, 0, None),
            artefacts=tuple(Artefact(m, False, 0, 0, None) for m in MODULES),
            export=ExportState(False, None, None, 0, 0),
            undeclared=(),
        )

        text = "\n".join(render(status))
        assert "twair ingest airtw" in text
        assert "twair export web" in text

    def test_the_store_span_counts_years_present_rather_than_the_range(self) -> None:
        """A gap must not be papered over: 1982 and 2025 alone is two years."""
        assert StoreState(True, (1982, 2025), 2, None, 0, None).span == "1982-2025 (2 years)"

        full = tuple(range(1982, 2026))
        assert StoreState(True, full, 521, None, 0, None).span == "1982-2025 (44 years)"

    def test_an_empty_store_says_so_rather_than_showing_a_broken_range(self) -> None:
        assert StoreState(True, (), 0, None, 0, None).span == "empty"


@pytest.mark.slow
def test_collect_status_runs_against_the_real_store() -> None:
    """The whole thing, on real data — it is meant to be run without thinking."""
    status = collect_status(count_rows=False)

    assert render(status)
