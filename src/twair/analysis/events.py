"""The policy-event timeline, and the gate in front of it.

`conf/events.yaml` feeds M5, where an event's date decides which observations
count as "before" and which as "after". A date that is wrong by a month aligns
the study to nothing and reports the noise as an effect — and unlike most bugs
in this project, that one produces a plausible number rather than a crash.

So the loader defaults to **verified events only**. Every entry carries a
`verified` flag that means three specific things: someone opened the cited page,
the page really records that date, and the page is still live. The first
compilation of this file had six entries and five failed at least one of those
tests, which is why the gate exists rather than a convention.

Unverified entries are kept in the file — several are probably correct — but
reaching them requires asking for them by name.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import polars as pl

from twair.config import ConfigError, load_conf

log = logging.getLogger(__name__)

__all__ = ["EVENT_FIELDS", "load_events", "verification_summary"]

EVENT_FIELDS = (
    "name",
    "name_en",
    "kind",
    "start",
    "end",
    "scope",
    "where",
    "expected_effect",
    "verified",
    "source_quality",
    "source_url",
    "source_url_end",
    "note",
)

# How authoritative the citation is. Kept separate from `verified` because
# "I checked it" and "it is authoritative" are different claims, and collapsing
# them would hide which events rest on a secondary account.
SOURCE_QUALITY = ("official", "news", "tertiary")


def load_events(
    *,
    include_unverified: bool = False,
    config: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Events from ``conf/events.yaml``.

    Verified only unless ``include_unverified`` is set, which exists for
    inspecting the backlog rather than for analysis. An unverified event with
    no start date is dropped even then: there is nothing to align to.
    """
    conf = config if config is not None else load_conf("events")
    entries = conf.get("events")
    if not entries:
        raise ConfigError("conf/events.yaml defines no `events`")

    for i, entry in enumerate(entries):
        missing = [f for f in ("name", "verified") if f not in entry]
        if missing:
            raise ConfigError(f"conf/events.yaml entry {i} is missing {missing}")
        if not entry["verified"]:
            continue

        name = entry["name"]
        if not entry.get("source_url"):
            raise ConfigError(
                f"conf/events.yaml: {name!r} is marked verified but has no source_url. "
                f"Verified means a page was opened and confirmed to record this date."
            )
        # An end date is a separate factual claim from a start date and usually
        # comes from a different announcement — the extension notice, not the
        # one that began the thing. It needs its own citation.
        if entry.get("end") is not None and not entry.get("source_url_end"):
            raise ConfigError(
                f"conf/events.yaml: {name!r} is verified and has an end date but no "
                f"source_url_end. The end of an event is its own claim."
            )
        quality = entry.get("source_quality")
        if quality not in SOURCE_QUALITY:
            raise ConfigError(
                f"conf/events.yaml: {name!r} is verified but source_quality is {quality!r}; "
                f"expected one of {SOURCE_QUALITY}."
            )

    frame = pl.DataFrame(
        [{field: entry.get(field) for field in EVENT_FIELDS} for entry in entries],
        schema={
            "name": pl.Utf8,
            "name_en": pl.Utf8,
            "kind": pl.Utf8,
            "start": pl.Date,
            "end": pl.Date,
            "scope": pl.Utf8,
            "where": pl.Utf8,
            "expected_effect": pl.Utf8,
            "verified": pl.Boolean,
            "source_quality": pl.Utf8,
            "source_url": pl.Utf8,
            "source_url_end": pl.Utf8,
            "note": pl.Utf8,
        },
    )

    if not include_unverified:
        withheld = frame.filter(~pl.col("verified"))
        if not withheld.is_empty():
            log.info(
                "%d unverified event(s) withheld from analysis: %s",
                withheld.height,
                withheld["name"].to_list(),
            )
        frame = frame.filter(pl.col("verified"))

    return frame.filter(pl.col("start").is_not_null()).sort("start")


def verification_summary(config: dict[str, Any] | None = None) -> pl.DataFrame:
    """What is usable and what still needs a source — the backlog, as a table."""
    conf = config if config is not None else load_conf("events")
    rows = []
    for entry in conf.get("events") or []:
        rows.append(
            {
                "name": entry.get("name"),
                "verified": bool(entry.get("verified")),
                "has_start": entry.get("start") is not None,
                "source_url": entry.get("source_url"),
                "why_not": None if entry.get("verified") else entry.get("verified_note"),
            }
        )
    return pl.DataFrame(rows)


def window(event: dict[str, Any], *, days: int = 90) -> tuple[date, date, date, date]:
    """Before/after windows around an event, for an event study.

    Returns ``(before_start, before_end, after_start, after_end)``. The event
    day itself belongs to neither: whatever happened on the announcement date
    is a mixture of both regimes.
    """
    from datetime import timedelta

    start = event["start"]
    if start is None:
        raise ValueError(f"{event.get('name')!r} has no start date to align to")

    after_from = event.get("end") or start
    return (
        start - timedelta(days=days),
        start - timedelta(days=1),
        after_from + timedelta(days=1),
        after_from + timedelta(days=days),
    )
