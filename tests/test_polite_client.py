"""The one door every byte from a government server comes through.

`net.py`'s docstring makes politeness and provenance 「properties of the system
rather than of whoever wrote a particular ingest module」. It was at 45%, and the
untested half was the politeness: which failures get repeated, how often, and
how fast.

They were repeated indiscriminately. The comment in `get` read 「4xx other than
429 are not worth retrying — fail fast with a clear error」, and the code retried
a 404 five times over thirty seconds, because the `if` beneath that comment
called `raise_for_status` in both branches and a type-based predicate cannot see
a status code.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from twair.net import (
    DownloadContractError,
    PoliteClient,
    _HostThrottle,
    _is_retryable,
    is_cached,
    manifest_lookup,
    quiet_http,
    read_manifest,
    record_download,
    sha256_file,
)

URL = "https://airtw.moenv.gov.tw/probe"


class _InterruptingStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"partial"
        raise KeyboardInterrupt


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tenacity's backoff out of the wall clock.

    The retry policy is 2, 4, 8, 16 seconds, which is the right policy and the
    wrong thing to sit through five times per test. Patched on the `Retrying`
    object the decorator attached, so the *counts* below are the real ones.
    """
    for method in (PoliteClient.get, PoliteClient.post):
        # `.retry` is the `Retrying` object tenacity attaches to the wrapped
        # function; it is untyped in the stubs, hence the cast.
        monkeypatch.setattr(cast(Any, method).retry, "sleep", lambda _: None)


@pytest.fixture
def client() -> Iterator[PoliteClient]:
    with PoliteClient(min_interval=0.0) as c:
        yield c


def counted(status: int) -> list[int]:
    """Mock the endpoint with a fixed status, returning the call tally."""
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(status)
        return httpx.Response(status)

    respx.get(URL).mock(side_effect=handler)
    return calls


# ── which failures are worth repeating ───────────────────────────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
def test_a_clear_no_is_not_asked_again(status: int) -> None:
    """A 404 is still a 404 in sixteen seconds.

    Measured before the fix: five requests and thirty seconds of backoff, all
    four extra ones spent on a server that had already answered. `twair verify`
    with a wrong key hit this once per source.
    """
    with respx.mock:
        calls = counted(status)
        with PoliteClient(min_interval=0.0) as c, pytest.raises(httpx.HTTPStatusError):
            c.get(URL)

    assert len(calls) == 1, f"{status} was retried {len(calls)} times"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_later_is_asked_again(status: int, no_waiting: None) -> None:
    """429 and 5xx are the two that mean the answer might change."""
    with respx.mock:
        calls = counted(status)
        with PoliteClient(min_interval=0.0) as c, pytest.raises(httpx.HTTPStatusError):
            c.get(URL)

    assert len(calls) == 5, f"{status} got {len(calls)} attempt(s), not the full five"


def test_a_dropped_connection_is_asked_again(no_waiting: None) -> None:
    """The failure the retry was written for: the network, not the answer."""
    with respx.mock:
        calls: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                raise httpx.ConnectError("connection reset")
            return httpx.Response(200, text="ok")

        respx.get(URL).mock(side_effect=handler)
        with PoliteClient(min_interval=0.0) as c:
            assert c.get_text(URL) == "ok"

    assert len(calls) == 3


def test_the_predicate_ignores_everything_that_is_not_an_http_failure() -> None:
    """A bug in a caller must not be retried five times as if it were weather."""
    assert not _is_retryable(ValueError("bad argument"))
    assert not _is_retryable(KeyboardInterrupt())


def test_a_post_gets_the_same_policy(no_waiting: None) -> None:
    """airtw's year selector is a postback, so the archive listing goes by POST."""
    with respx.mock:
        calls: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(404)

        respx.post(URL).mock(side_effect=handler)
        with PoliteClient(min_interval=0.0) as c, pytest.raises(httpx.HTTPStatusError):
            c.post(URL, data={"__VIEWSTATE": "x"})

    assert len(calls) == 1, "a 404 postback was repeated"


def test_the_postback_carries_its_form_and_keeps_the_cookie(client: PoliteClient) -> None:
    """The 「歷年」 listing only exists behind a __VIEWSTATE POST with a session."""
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, text="<table>…</table>", headers={"set-cookie": "ASP.NET_SessionId=abc"}
            )
        )
        assert "<table>" in client.post(URL, data={"__VIEWSTATE": "x"}).text

    # The session cookie has to survive to the next postback, or every year of
    # the archive listing comes back as the default year.
    assert client._client.cookies.get("ASP.NET_SessionId") == "abc"


def test_get_json_is_the_same_request_with_the_body_parsed(client: PoliteClient) -> None:
    with respx.mock:
        respx.get(URL).mock(
            return_value=httpx.Response(200, json={"records": [{"sitename": "板橋"}]})
        )
        assert client.get_json(URL)["records"][0]["sitename"] == "板橋"


# ── the per-host interval ────────────────────────────────────────────────────


def test_two_requests_to_one_host_are_spaced() -> None:
    throttle = _HostThrottle(0.2)
    throttle.wait("https://airtw.moenv.gov.tw/a")

    start = time.monotonic()
    throttle.wait("https://airtw.moenv.gov.tw/b")

    # Not asserted to the millisecond: Windows' timer granularity is ~15 ms and
    # `time.sleep` undershoots `time.monotonic` by a few of them (measured at
    # 47 ms against a 50 ms interval). The guarantee being pinned is that the
    # second request waits at all, not that it waits exactly.
    assert time.monotonic() - start >= 0.15


def test_a_second_host_does_not_wait_for_the_first() -> None:
    """Per-host, not global — otherwise one slow provider throttles the rest."""
    throttle = _HostThrottle(5.0)
    throttle.wait("https://airtw.moenv.gov.tw/a")

    start = time.monotonic()
    throttle.wait("https://data.moenv.gov.tw/a")

    assert time.monotonic() - start < 1.0


# ── the key that got logged once, to a real key ──────────────────────────────


def test_quiet_http_restores_whatever_level_it_found() -> None:
    """It silences httpx around requests that carry a secret in the URL.

    Leaving the logger raised afterwards would hide unrelated diagnostics for
    the rest of the run, so the restore matters as much as the silencing.
    """
    logger = logging.getLogger("httpx")
    before = logger.level
    try:
        logger.setLevel(logging.DEBUG)
        with quiet_http():
            assert logger.level == logging.WARNING
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(before)


def test_quiet_http_restores_even_when_the_request_fails() -> None:
    logger = logging.getLogger("httpx")
    before = logger.level
    try:
        logger.setLevel(logging.INFO)
        with pytest.raises(RuntimeError), quiet_http():
            raise RuntimeError("timed out mid-request")
        assert logger.level == logging.INFO
    finally:
        logger.setLevel(before)


# ── the ledger, which is the provenance half ─────────────────────────────────


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    return tmp_path


def written(ledger: Path, name: str, body: bytes) -> Path:
    path = ledger / name
    path.write_bytes(body)
    return path


def test_a_download_is_recorded_with_its_hash_and_size(ledger: Path) -> None:
    path = written(ledger, "a.zip", b"PK\x03\x04payload")

    entry = record_download(
        key="airtw/2024/hourly/離島", url="https://x/y", path=path, source="airtw"
    )

    assert entry["bytes"] == len(b"PK\x03\x04payload")
    assert entry["sha256"] == sha256_file(path)
    assert entry["fetched_at"].endswith("+00:00"), "provenance without a timezone is not provenance"
    assert list(read_manifest()) == [entry]


def test_extra_provenance_travels_with_the_entry(ledger: Path) -> None:
    """`download_one` records the year and station group it asked for.

    The ledger is the only thing that says which archive a file on disk came
    from; the filename is a convention and the Drive id rotates.
    """
    path = written(ledger, "a.zip", b"PK\x03\x04")

    entry = record_download(
        key="airtw/2024/hourly/離島",
        url="https://x/y",
        path=path,
        source="airtw",
        extra={"year": 2024, "station_group": "離島", "drive_file_id": "abc"},
    )

    assert manifest_lookup("airtw/2024/hourly/離島") == entry
    assert entry["year"] == 2024
    assert entry["station_group"] == "離島"


def test_the_ledger_is_append_only_and_the_last_entry_wins(ledger: Path) -> None:
    """Re-downloading rewrites nothing; the history of a key stays readable."""
    first = written(ledger, "a.zip", b"one")
    record_download(key="k", url="u", path=first, source="airtw")
    second = written(ledger, "a.zip", b"two!!")
    record_download(key="k", url="u", path=second, source="airtw")

    assert len(list(read_manifest())) == 2
    assert manifest_lookup("k")["bytes"] == 5  # type: ignore[index]


def test_a_missing_ledger_yields_nothing_rather_than_raising(ledger: Path) -> None:
    """A fresh clone has no ledger, and every `is_cached` call goes through it."""
    assert list(read_manifest()) == []
    assert manifest_lookup("anything") is None
    assert is_cached("anything") is None


def test_a_file_that_shrank_is_not_treated_as_cached(ledger: Path) -> None:
    """The interrupted-download case, which the size is there to catch."""
    path = written(ledger, "a.zip", b"the whole archive")
    record_download(key="k", url="u", path=path, source="airtw")
    assert is_cached("k") == path

    path.write_bytes(b"trunc")
    assert is_cached("k") is None


def test_a_deleted_file_is_not_treated_as_cached(ledger: Path) -> None:
    path = written(ledger, "a.zip", b"gone soon")
    record_download(key="k", url="u", path=path, source="airtw")
    path.unlink()

    assert is_cached("k") is None


# ── the streaming download ───────────────────────────────────────────────────


def test_a_stream_lands_atomically(ledger: Path, client: PoliteClient) -> None:
    """`.part` then rename, so a half-written file never has the real name."""
    dest = ledger / "nested" / "a.zip"
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, content=b"PK\x03\x04body"))
        assert client.stream_to_file(URL, dest) == dest

    assert dest.read_bytes() == b"PK\x03\x04body"
    assert not dest.with_suffix(".zip.part").exists(), "the part file outlived the download"


def test_a_stream_that_fails_leaves_no_file_under_the_real_name(
    ledger: Path, client: PoliteClient
) -> None:
    """A truncated archive under the final name is the worst outcome here.

    Everything downstream — `is_cached`, the archive magic check, the parser —
    trusts that a file at that path is a file that finished arriving.
    """
    dest = ledger / "a.zip"
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            client.stream_to_file(URL, dest)

    assert not dest.exists()


def test_a_stream_rejects_an_unreviewed_content_type_before_writing(
    ledger: Path, client: PoliteClient
) -> None:
    dest = ledger / "a.zip"
    with respx.mock:
        respx.get(URL).mock(
            return_value=httpx.Response(
                200,
                content=b"PK\x03\x04body",
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        )
        with pytest.raises(DownloadContractError, match="content type"):
            client.stream_to_file(
                URL,
                dest,
                allowed_content_types=frozenset({"application/zip"}),
            )

    assert not dest.exists()
    assert not dest.with_suffix(".zip.part").exists()


def test_a_declared_stream_beyond_the_byte_ceiling_is_rejected_before_writing(
    ledger: Path, client: PoliteClient
) -> None:
    dest = ledger / "a.zip"
    with respx.mock:
        respx.get(URL).mock(
            return_value=httpx.Response(
                200,
                stream=httpx.ByteStream(b"too large"),
                headers={"Content-Length": "9"},
            )
        )
        with pytest.raises(DownloadContractError, match="maximum byte count"):
            client.stream_to_file(URL, dest, max_bytes=8)

    assert not dest.exists()
    assert not dest.with_suffix(".zip.part").exists()


def test_a_stream_without_content_length_is_accepted_only_after_exact_counting(
    ledger: Path, client: PoliteClient
) -> None:
    dest = ledger / "a.zip"
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, stream=httpx.ByteStream(b"12345678")))
        assert client.stream_to_file(URL, dest, expected_bytes=8, max_bytes=8) == dest

    assert dest.read_bytes() == b"12345678"


def test_an_unannounced_stream_overrun_removes_the_partial_file(
    ledger: Path, client: PoliteClient
) -> None:
    dest = ledger / "a.zip"
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, stream=httpx.ByteStream(b"123456789")))
        with pytest.raises(DownloadContractError, match="maximum byte count"):
            client.stream_to_file(URL, dest, max_bytes=8)

    assert not dest.exists()
    assert not dest.with_suffix(".zip.part").exists()


def test_an_exact_byte_mismatch_never_replaces_an_existing_destination(
    ledger: Path, client: PoliteClient
) -> None:
    dest = written(ledger, "a.zip", b"reviewed old bytes")
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, stream=httpx.ByteStream(b"short")))
        with pytest.raises(DownloadContractError, match="expected byte count"):
            client.stream_to_file(URL, dest, expected_bytes=8)

    assert dest.read_bytes() == b"reviewed old bytes"
    assert not dest.with_suffix(".zip.part").exists()


def test_a_base_exception_removes_only_the_partial_file(ledger: Path, client: PoliteClient) -> None:
    dest = written(ledger, "a.zip", b"reviewed old bytes")
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, stream=_InterruptingStream()))
        with pytest.raises(KeyboardInterrupt):
            client.stream_to_file(URL, dest)

    assert dest.read_bytes() == b"reviewed old bytes"
    assert not dest.with_suffix(".zip.part").exists()
