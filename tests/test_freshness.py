"""Tests for the staleness check.

The first version of this compared the published data against a live hourly
feed and called anything over 45 days behind stale. It fired on the first run
and would have fired every day forever, because the source archives are annual
and the site is structurally months behind live. A check that always fires is
the same as no check.

So the arithmetic is what gets pinned here: a year counts as expected only once
it has *finished* plus a grace period. The corrected version asked for 2026 in
July 2026 — a year that had not ended — which is the exact off-by-twelve these
cases exist to catch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from twair.freshness import (
    LIVE_ENDPOINT,
    FreshnessReport,
    latest_upstream,
    parse_publish_time,
    read_data_through,
)


def _report(
    *, through: datetime | None, checked: datetime, live: datetime | None = None
) -> FreshnessReport:
    return FreshnessReport(data_through=through, published_at=live, checked_at=checked)


class TestWhichYearIsExpected:
    @pytest.mark.parametrize(
        ("checked", "expected"),
        [
            # Mid-2026, six months' grace: 2025 finished long enough ago.
            (datetime(2026, 7, 29, tzinfo=UTC), 2025),
            # January 2026 — 2025 ended a month ago, too soon to expect it.
            (datetime(2026, 1, 15, tzinfo=UTC), 2024),
            # July is exactly the grace boundary for the previous year.
            (datetime(2026, 6, 30, tzinfo=UTC), 2024),
            (datetime(2026, 12, 31, tzinfo=UTC), 2025),
        ],
    )
    def test_the_grace_period_is_measured_from_the_year_end(
        self, checked: datetime, expected: int
    ) -> None:
        assert _report(through=None, checked=checked).expected_year == expected

    def test_it_never_expects_a_year_that_has_not_ended(self) -> None:
        """The bug: asking for 2026's archive in July 2026."""
        for month in range(1, 13):
            checked = datetime(2026, month, 15, tzinfo=UTC)
            assert _report(through=None, checked=checked).expected_year < 2026


class TestStaleness:
    def test_a_complete_previous_year_is_not_stale(self) -> None:
        report = _report(
            through=datetime(2025, 12, 31, 23, tzinfo=UTC),
            checked=datetime(2026, 7, 29, tzinfo=UTC),
        )

        assert not report.is_stale
        assert report.missing_years == []

    def test_a_missing_year_is_named(self) -> None:
        report = _report(
            through=datetime(2024, 12, 31, 23, tzinfo=UTC),
            checked=datetime(2026, 7, 29, tzinfo=UTC),
        )

        assert report.is_stale
        assert report.missing_years == [2025]

    def test_several_missing_years_are_all_named(self) -> None:
        report = _report(
            through=datetime(2022, 12, 31, 23, tzinfo=UTC),
            checked=datetime(2026, 7, 29, tzinfo=UTC),
        )

        assert report.missing_years == [2023, 2024, 2025]

    def test_a_partial_final_year_does_not_count_as_having_it(self) -> None:
        """Data ending in March 2025 is not "2025 ingested"."""
        report = _report(
            through=datetime(2025, 3, 31, tzinfo=UTC),
            checked=datetime(2026, 7, 29, tzinfo=UTC),
        )

        assert report.missing_years == [2025]

    def test_being_far_behind_the_live_feed_is_not_staleness(self) -> None:
        """The whole correction, as an assertion.

        Two hundred days behind an hourly feed is the normal state of a site
        built from annual archives. Treating that as an alarm is what made the
        first version useless.
        """
        report = _report(
            through=datetime(2025, 12, 31, 23, tzinfo=UTC),
            checked=datetime(2026, 7, 29, tzinfo=UTC),
            live=datetime(2026, 7, 29, 15, tzinfo=UTC),
        )

        assert report.lag_days is not None and report.lag_days > 200
        assert not report.is_stale

    def test_the_summary_says_what_to_run(self) -> None:
        report = _report(
            through=datetime(2024, 12, 31, tzinfo=UTC),
            checked=datetime(2026, 7, 29, tzinfo=UTC),
        )

        assert "twair ingest airtw" in report.summary()


class TestNotBeingAbleToAnswer:
    """ "No answer" must not be readable as "no problem".

    `viz.export._data_through` catches a bare Exception and returns None, so an
    export can lose the field without failing. If that state were reported the
    same way as a current export, the weekly job would stay green forever while
    measuring nothing.
    """

    def test_an_export_with_no_date_is_unknown(self) -> None:
        assert _report(through=None, checked=datetime(2026, 7, 29, tzinfo=UTC)).is_unknown

    def test_unknown_is_not_the_same_state_as_stale(self) -> None:
        """Both mean "do not trust this", but only stale names a year to fetch."""
        report = _report(through=None, checked=datetime(2026, 7, 29, tzinfo=UTC))

        assert report.is_unknown
        assert not report.is_stale
        assert report.missing_years == []

    def test_a_dated_export_is_not_unknown(self) -> None:
        report = _report(
            through=datetime(2025, 12, 31, 23, tzinfo=UTC),
            checked=datetime(2026, 7, 29, tzinfo=UTC),
        )

        assert not report.is_unknown

    def test_the_summary_says_how_to_answer_the_question(self) -> None:
        report = _report(through=None, checked=datetime(2026, 7, 29, tzinfo=UTC))

        assert "twair export web" in report.summary()


class TestTheKeyNeverReachesTheLog:
    """MOENV takes the credential as a query parameter.

    That gives it two independent routes into a log: httpx writes the full
    request URL at INFO, and HTTPStatusError puts the same URL in its own
    message. A leak of exactly this shape has already happened once in this
    repo, to a real key, which is why `net.quiet_http` exists. Both routes are
    closed and both are pinned here, because the scheduled workflow runs this
    code with a repository secret and writes its output to a build log.
    """

    KEY = "secret-key-must-not-appear"

    def test_the_httpx_logger_is_silenced_while_the_request_is_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        levels: list[int] = []

        def _get(url: str, **kwargs: object) -> httpx.Response:
            levels.append(logging.getLogger("httpx").getEffectiveLevel())
            return httpx.Response(200, json={"records": []}, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", _get)
        latest_upstream(self.KEY)

        assert levels, "the request was never made"
        assert levels[0] >= logging.WARNING

    def test_a_rejected_request_does_not_put_the_key_in_the_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The status survives; the URL that carries the key does not."""
        request = httpx.Request("GET", f"{LIVE_ENDPOINT}?api_key={self.KEY}&format=json")

        def _get(url: str, **kwargs: object) -> httpx.Response:
            raise httpx.HTTPStatusError(
                f"Client error '401 Unauthorized' for url '{request.url}'",
                request=request,
                response=httpx.Response(401, request=request),
            )

        monkeypatch.setattr(httpx, "get", _get)

        with caplog.at_level(logging.DEBUG):
            assert latest_upstream(self.KEY) is None

        assert self.KEY not in caplog.text
        assert "401" in caplog.text

    def test_a_transport_failure_does_not_put_the_key_in_the_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _get(url: str, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError(f"failed to reach {LIVE_ENDPOINT}?api_key={self.KEY}")

        monkeypatch.setattr(httpx, "get", _get)

        with caplog.at_level(logging.DEBUG):
            assert latest_upstream(self.KEY) is None

        assert self.KEY not in caplog.text


class TestPublishTime:
    def test_moenv_uses_slashes(self) -> None:
        assert parse_publish_time("2026/07/29 15:00:00") == datetime(2026, 7, 29, 15, tzinfo=UTC)

    def test_dashes_are_accepted_too(self) -> None:
        assert parse_publish_time("2026-07-29 15:00:00") == datetime(2026, 7, 29, 15, tzinfo=UTC)

    def test_an_unparseable_stamp_returns_none_rather_than_guessing(self) -> None:
        assert parse_publish_time("last Tuesday") is None

    def test_the_result_is_timezone_aware(self) -> None:
        """Subtracting a naive from an aware datetime raises; both sides must
        be aware or the check dies on its first real run."""
        parsed = parse_publish_time("2026/07/29 15:00:00")

        assert parsed is not None and parsed.tzinfo is not None


class TestReadingTheCommittedExport:
    def test_a_missing_meta_file_is_not_an_error(self, tmp_path: Path) -> None:
        """CI checks out a repo that may not have run an export."""
        assert read_data_through(tmp_path / "absent.json") is None

    def test_an_unparseable_value_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        path.write_text('{"data_through": "soon"}', encoding="utf-8")

        assert read_data_through(path) is None

    def test_a_naive_timestamp_is_made_aware(self, tmp_path: Path) -> None:
        path = tmp_path / "meta.json"
        path.write_text('{"data_through": "2025-12-31 23:00:00"}', encoding="utf-8")

        result = read_data_through(path)

        assert result is not None and result.tzinfo is not None
