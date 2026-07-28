"""Tests for the policy-event timeline and its verification gate.

The gate exists because the first compilation of `conf/events.yaml` had six
entries and five of them cited pages that did not record the dates claimed —
including four sharing one URL that turned out to be a statute's amendment
history, containing nothing about power plants, subsidies or plant maintenance.

A wrong event date does not crash anything. It aligns an event study to the
wrong week and reports the noise as a policy effect. So these tests are about
making that failure impossible to reach by accident.
"""

from __future__ import annotations

from datetime import date

import pytest

from twair.analysis.events import load_events, verification_summary, window
from twair.config import ConfigError

VERIFIED = {
    "name": "已查證事件",
    "name_en": "verified event",
    "kind": "policy",
    "start": date(2018, 8, 1),
    "end": None,
    "scope": "national",
    "where": "全國",
    "expected_effect": "decrease",
    "verified": True,
    "source_quality": "official",
    "source_url": "https://example.gov.tw/real-page",
    "source_url_end": None,
    "note": "",
}

UNVERIFIED = {
    **VERIFIED,
    "name": "未查證事件",
    "start": date(2021, 5, 19),
    "verified": False,
    "source_url": None,
}


class TestVerificationGate:
    def test_unverified_events_are_withheld_by_default(self) -> None:
        events = load_events(config={"events": [VERIFIED, UNVERIFIED]})

        assert events["name"].to_list() == ["已查證事件"]

    def test_they_can_be_requested_explicitly_for_inspection(self) -> None:
        events = load_events(config={"events": [VERIFIED, UNVERIFIED]}, include_unverified=True)

        assert set(events["name"]) == {"已查證事件", "未查證事件"}

    def test_claiming_verified_without_a_source_is_rejected(self) -> None:
        """The exact shape of the failure this gate was built for.

        An entry that says `verified: true` and cites nothing is worse than an
        unverified one, because it passes the filter.
        """
        liar = {**VERIFIED, "source_url": None}

        with pytest.raises(ConfigError, match="verified but has no source_url"):
            load_events(config={"events": [liar]})

    def test_an_end_date_needs_its_own_source(self) -> None:
        """The end of an event is a separate factual claim from its start.

        Taiwan's Level 3 alert began in one CECC announcement and ended in a
        different one issued seven weeks later. Citing only the first would
        source the start and leave the end unevidenced.
        """
        half_sourced = {**VERIFIED, "end": date(2021, 7, 26), "source_url_end": None}

        with pytest.raises(ConfigError, match="end of an event is its own claim"):
            load_events(config={"events": [half_sourced]})

    def test_a_verified_entry_must_declare_how_good_its_source_is(self) -> None:
        """ "I checked it" and "it is authoritative" are different claims."""
        unlabelled = {**VERIFIED, "source_quality": "probably fine"}

        with pytest.raises(ConfigError, match="source_quality"):
            load_events(config={"events": [unlabelled]})

    def test_an_entry_without_a_verified_field_is_rejected(self) -> None:
        """Absent must not default to usable."""
        entry = {k: v for k, v in VERIFIED.items() if k != "verified"}

        with pytest.raises(ConfigError, match="missing"):
            load_events(config={"events": [entry]})

    def test_an_event_with_no_start_date_cannot_be_analysed(self) -> None:
        """There is nothing to align before and after to."""
        undated = {**UNVERIFIED, "name": "無日期", "start": None}

        events = load_events(config={"events": [undated]}, include_unverified=True)

        assert events.is_empty()


class TestShippedTimeline:
    def test_every_verified_event_in_the_shipped_config_cites_a_source(self) -> None:
        events = load_events()

        assert events["source_url"].is_not_null().all()

    def test_the_backlog_explains_why_each_entry_is_unverified(self) -> None:
        """An unverified entry with no explanation is just an unexplained date."""
        summary = verification_summary()
        pending = summary.filter(~summary["verified"])

        assert pending["why_not"].is_not_null().all()


class TestEventWindow:
    def test_the_event_day_belongs_to_neither_window(self) -> None:
        _, before_end, after_start, _ = window(VERIFIED, days=30)

        assert before_end < VERIFIED["start"]
        assert after_start > VERIFIED["start"]

    def test_a_span_measures_after_from_its_end_not_its_start(self) -> None:
        """A 70-day lockdown's 'after' begins when it lifts, not when it starts."""
        spanning = {**VERIFIED, "start": date(2021, 5, 19), "end": date(2021, 7, 26)}

        _, _, after_start, _ = window(spanning, days=30)

        assert after_start == date(2021, 7, 27)

    def test_an_undated_event_raises_rather_than_guessing(self) -> None:
        with pytest.raises(ValueError, match="no start date"):
            window({**VERIFIED, "start": None})
