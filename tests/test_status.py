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

from twair.freshness import FreshnessReport
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


def _artefact(
    name: str, *, modified: datetime | None, exists: bool = True, feeds_web: bool = True
) -> Artefact:
    return Artefact(
        Module(name, f"twair analyze {name}", name, feeds_web=feeds_web), exists, 3, 1024, modified
    )


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

        `m2_drivers` is why this was written: it used to be produced by a loose
        script rather than a subcommand, which is invisible from the output
        directory and impossible to guess two weeks later. It is
        `twair analyze m2` now, and this check is what makes that survive the
        next rename.
        """
        missing = [target for target, found in declared_reproduce_targets().items() if not found]

        assert not missing, f"declared but not found: {missing}"

    def test_module_directories_are_unique(self) -> None:
        names = [m.directory for m in MODULES]

        assert len(names) == len(set(names))

    def test_m8_satellite_has_a_real_return_command_but_does_not_yet_feed_the_site(
        self,
    ) -> None:
        module = next(item for item in MODULES if item.directory == "m8_satellite")

        assert module.reproduce == "twair analyze m8"
        assert module.feeds_web is False
        assert "not calibration" in module.what

    def test_the_era5_value_add_has_a_return_command_but_does_not_yet_feed_the_site(
        self,
    ) -> None:
        module = next(item for item in MODULES if item.directory == "m8_era5_value")

        assert module.reproduce == "twair analyze era5-value"
        assert module.feeds_web is False
        assert "held-out" in module.what

    def test_era5_robustness_has_a_reproducible_non_web_status_entry(self) -> None:
        module = next(item for item in MODULES if item.directory == "m8_era5_robustness")

        assert module.reproduce == "twair analyze era5-robustness"
        assert module.feeds_web is False

    def test_satellite_value_has_a_reproducible_non_web_status_entry(self) -> None:
        module = next(item for item in MODULES if item.directory == "m8_satellite_value")

        assert module.reproduce == (
            "twair analyze satellite-value "
            "--generation 58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788 "
            "--year 2025"
        )
        assert declared_reproduce_targets()[module.reproduce] is True
        assert module.feeds_web is False
        assert "held-out" in module.what

    def test_satellite_robustness_has_an_exact_non_web_return_command(self) -> None:
        module = next(item for item in MODULES if item.directory == "m8_satellite_robustness")

        assert module.reproduce == (
            "twair analyze satellite-robustness "
            "--generation 58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788"
        )
        assert declared_reproduce_targets()[module.reproduce] is True
        assert module.feeds_web is False
        assert "multi-year" in module.what

    def test_an_undeclared_output_directory_is_reported_rather_than_ignored(self) -> None:
        """A new module that nobody wrote down is the failure being guarded."""
        status = Status(
            store=StoreState(False, (), 0, None, 0, None),
            artefacts=(),
            export=ExportState(False, None, None, 0, 0),
            undeclared=("m11_mystery",),
        )

        assert any("undeclared" in line for line in render(status))


class TestTheUpstreamCheckStaysOffline:
    """`status` is the command you run without thinking about it.

    `check_freshness` has a 30-second timeout. Reaching it from here would mean
    that on a plane, behind a firewall, or with no key configured, the fast
    command sits silent for half a minute — and then people stop running it,
    which is the outcome this module was written to prevent. Only the offline
    half is wired in, and the obvious "simplification" is to call
    `check_freshness` instead, so the property is pinned rather than intended.
    """

    def test_collecting_status_never_makes_a_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        def _forbidden(*args: object, **kwargs: object) -> object:
            raise AssertionError("twair status made an HTTP request")

        monkeypatch.setattr(httpx, "get", _forbidden)
        monkeypatch.setattr(httpx, "request", _forbidden)
        monkeypatch.setattr(httpx.Client, "send", _forbidden)

        assert render(collect_status(count_rows=False))

    def test_a_year_published_upstream_reaches_the_next_steps(self) -> None:
        status = Status(
            store=StoreState(True, (), 0, None, 0, None),
            artefacts=(),
            export=ExportState(True, datetime(2026, 7, 29, tzinfo=UTC), "abc1234", 84, 1),
            undeclared=(),
            freshness=FreshnessReport(
                data_through=datetime(2024, 12, 31, 23, tzinfo=UTC),
                published_at=None,
                checked_at=datetime(2026, 7, 29, tzinfo=UTC),
            ),
        )

        assert any("2025" in step for step in status.next_steps())
        assert any("STALE: 2025" in line for line in render(status))

    def test_an_unmeasured_upstream_is_not_rendered_as_a_problem(self) -> None:
        """``None`` means "not measured", which is not the same as "behind"."""
        status = _status(export_at=datetime(2026, 7, 29, tzinfo=UTC))

        assert status.freshness is None
        assert status.next_steps() == []
        assert any("not measured" in line for line in render(status))


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

    def test_a_module_the_export_does_not_read_is_never_stale(self) -> None:
        """Otherwise the line cries wolf forever.

        `qc` writes docs/data-quality.md and `qc_outliers` writes a parquet
        nothing on the site imports, so running either would leave `status`
        advising an export that could not change a byte. This module already
        argues that a line people learn to ignore is worse than no line.
        """
        status = _status(
            _artefact("qc_outliers", modified=datetime(2026, 7, 29, tzinfo=UTC), feeds_web=False),
            export_at=datetime(2026, 7, 28, tzinfo=UTC),
        )

        assert status.stale_export == ()
        assert status.next_steps() == []

    def test_the_two_modules_the_site_does_not_read_are_marked_as_such(self) -> None:
        by_name = {module.directory: module for module in MODULES}

        assert by_name["qc"].feeds_web is False
        assert by_name["qc_outliers"].feeds_web is False
        assert by_name["m1_replication"].feeds_web is True


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
    def test_a_current_project_points_to_the_portable_public_return_path(self) -> None:
        exported = datetime(2026, 7, 28, tzinfo=UTC)
        status = _status(
            _artefact("m4_deweather", modified=exported - timedelta(days=1)), export_at=exported
        )

        text = "\n".join(render(status))

        assert "PLAN.md" in text
        assert "docs/working-rules.md" in text
        assert "PROGRESS.md" not in text
        assert "HANDOFF.md" not in text

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
