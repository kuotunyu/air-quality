"""How far behind upstream the published data is.

The original blueprint asked for a daily job that fetches increments, re-runs QC, rebuilds the
Parquet store and redeploys. That cannot be built here, and the reason is
already written down at the `deploy` job in `.github/workflows/ci.yml`: **CI has no copy of the
341-million-row store.** A workflow that claimed to refresh it would either
have to ship 605 MB into the runner on every run or maintain a second,
divergent data path — and a second path that can silently disagree with the
first is precisely the class of defect this project exists to document.

So the automation measures instead of pretending. This module answers one
question — *how stale is what we published?* — by comparing two things that are
both cheaply available without the store:

``data_through``
    the newest observation the export was built from, written into
    `web/public/data/meta.json` and committed alongside the site.

``published_at``
    the newest hourly reading MOENV is currently serving from its live API.

The gap between them is the answer. It needs no local data, so it runs in CI,
and it turns "should I refresh?" from something to remember into something
measured — the same split as `twair status`, one step further out.

A refresh remains a **local** step: `twair ingest airtw && twair build &&
twair aggregate && twair export web`, then commit. This only says when.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["FreshnessReport", "check_freshness", "parse_publish_time", "read_data_through"]

# MOENV's "AQI by station" resource — current hourly readings for every site.
# Chosen over the annual archives because it is the only endpoint that answers
# "what is the newest hour that exists upstream" in one request.
LIVE_ENDPOINT = "https://data.moenv.gov.tw/api/v2/aqx_p_432"

# How long after a calendar year ends before its archive is expected to exist.
#
# The first version of this compared `data_through` against the live API and
# called anything over 45 days stale. That was wrong in a way worth recording:
# **the archives are annual**, so the site is structurally months behind a live
# hourly feed and always will be. The check fired immediately and would have
# fired every day forever, which is the same as no check.
#
# The actionable question is not "how far behind live" but "has a completed
# year been published that we have not ingested". Six months is the grace
# period: long enough that MOENV has posted the previous year's archive, short
# enough that a missed year is caught within the same calendar year.
ARCHIVE_GRACE_MONTHS = 6


@dataclass(frozen=True, slots=True)
class FreshnessReport:
    data_through: datetime | None
    published_at: datetime | None
    checked_at: datetime

    @property
    def lag_days(self) -> float | None:
        """Distance to the live feed. Context, not an alarm — see the constant."""
        if self.data_through is None or self.published_at is None:
            return None
        return (self.published_at - self.data_through).total_seconds() / 86_400

    @property
    def expected_year(self) -> int:
        """The most recent calendar year whose archive should exist by now.

        A year Y finishes at the end of month ``Y * 12 + 12``, so it is only
        expected once that many months plus the grace period have elapsed. An
        earlier version dropped the twelve and asked for the *current* year in
        July, which no archive could ever satisfy.

        The month index has to be zero-based for that arithmetic to hold. A
        one-based month counts December of year Y as ``Y * 12 + 12`` — the
        month the year ends *in*, not the number of months elapsed — so every
        threshold arrived a month early and 2025 became "expected" on
        2026-06-01 instead of 2026-07-01. The 2026-06-30 case in
        ``test_the_grace_period_is_measured_from_the_year_end`` exists for
        exactly this, and caught it.
        """
        now = self.checked_at.year * 12 + (self.checked_at.month - 1)
        return (now - 12 - ARCHIVE_GRACE_MONTHS) // 12

    @property
    def missing_years(self) -> list[int]:
        """Completed years that ought to be ingested and are not."""
        if self.data_through is None:
            return []
        have_through = self.data_through.year
        if self.data_through.month < 12:
            # A partial final year does not count as having that year.
            have_through -= 1
        return list(range(have_through + 1, self.expected_year + 1))

    @property
    def is_stale(self) -> bool:
        return bool(self.missing_years)

    @property
    def is_unknown(self) -> bool:
        """The export cannot say how current it is.

        Deliberately separate from ``is_stale``, and it has to stay separate:
        stale names a year to go and fetch, unknown means the question was
        never answered. Both mean "do not trust this export", so both have to
        fail ``--fail-if-stale`` — but reporting them as the same state would
        lose the only one that tells you what to do about it.

        Reachable without anything failing loudly: ``viz.export._data_through``
        catches a bare ``Exception`` and returns ``None``, so a future export
        can drop the field and still succeed. Left indistinguishable from
        fresh, that would leave the weekly job green forever while measuring
        nothing.
        """
        return self.data_through is None

    def summary(self) -> str:
        if self.data_through is None:
            return "no data_through in meta.json — run `twair export web`"

        live = (
            f", live feed at {self.published_at:%Y-%m-%d %H:%M} "
            f"({self.lag_days:.0f}d ahead — normal, the archives are annual)"
            if self.published_at is not None and self.lag_days is not None
            else ", upstream did not answer"
        )

        if self.is_stale:
            years = ", ".join(str(y) for y in self.missing_years)
            return (
                f"STALE: data ends {self.data_through:%Y-%m-%d}, but {years} should be "
                f"published by now — run `twair ingest airtw && twair build`{live}"
            )
        return f"ok: data ends {self.data_through:%Y-%m-%d}, no completed year missing{live}"


def read_data_through(meta_path: Path | None = None) -> datetime | None:
    """The newest observation behind the committed export.

    Read from the committed `meta.json` rather than from the store, so this
    works in a checkout that has no data — which is every CI runner.
    """
    from twair.paths import WEB_DIR

    path = meta_path or WEB_DIR / "public" / "data" / "meta.json"
    if not path.exists():
        return None

    raw = json.loads(path.read_text(encoding="utf-8")).get("data_through")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        log.warning("meta.json has an unparseable data_through: %r", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_publish_time(value: str) -> datetime | None:
    """Parse MOENV's ``publishtime``, which is ``2026/07/29 15:00:00``.

    Slashes, not dashes, and no timezone — the readings are Taiwan local time.
    Returned as UTC-aware so the subtraction against ``data_through`` cannot
    silently compare a naive datetime with an aware one.
    """
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    log.warning("unrecognised publishtime: %r", value)
    return None


def latest_upstream(api_key: str, *, timeout: float = 30.0) -> datetime | None:
    """The newest ``publishtime`` MOENV is currently serving."""
    import httpx

    from twair.net import quiet_http

    try:
        # MOENV takes the key as a query parameter, and httpx logs the full
        # request URL at INFO — so an unwrapped call writes the credential to
        # the terminal, and in the scheduled workflow to a build log. This is
        # the second module to need `quiet_http`; its docstring asks to be used
        # by the next one, which is this one.
        with quiet_http():
            response = httpx.get(
                LIVE_ENDPOINT,
                params={"limit": 50, "api_key": api_key, "format": "json"},
                timeout=timeout,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # The status is the useful half; the exception's own message carries the
        # full URL, key included, so it must not be formatted into the log. That
        # is the same leak by a second route, past the logger just silenced.
        log.warning("upstream check failed: HTTP %s", exc.response.status_code)
        return None
    except httpx.HTTPError as exc:
        log.warning("upstream check failed: %s", type(exc).__name__)
        return None

    payload = response.json()
    records = payload if isinstance(payload, list) else payload.get("records", [])

    stamps = [
        parsed
        for record in records
        if (raw := record.get("publishtime"))
        and (parsed := parse_publish_time(str(raw))) is not None
    ]
    return max(stamps) if stamps else None


def check_freshness(meta_path: Path | None = None) -> FreshnessReport:
    """Compare the committed export against what upstream is serving now."""
    from twair.config import get_settings

    key = get_settings().moenv_api_key
    published = latest_upstream(key) if key else None
    if not key:
        log.warning("MOENV_API_KEY is not set — cannot ask upstream")

    return FreshnessReport(
        data_through=read_data_through(meta_path),
        published_at=published,
        checked_at=datetime.now(tz=UTC),
    )
