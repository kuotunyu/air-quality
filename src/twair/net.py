"""Polite HTTP access with retry, per-host rate limiting, and a download ledger.

Every byte this project pulls from a government server goes through here, so
that politeness and provenance are properties of the system rather than of
whoever wrote a particular ingest module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from twair.config import get_settings
from twair.paths import manifest_path

log = logging.getLogger(__name__)

RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


@contextmanager
def quiet_http() -> Iterator[None]:
    """Silence httpx request logging for the duration of a block.

    httpx logs the full request URL at INFO. Several providers here — CWA and
    MOENV among them — take their credential as a *query parameter*, so that
    log line writes the key into the terminal and into any file the run is
    piped to. It happened once, to a real key.

    Wrap every request that carries a secret in the URL. This lives in
    ``net`` rather than in one caller so that the next module to need it
    does not have to rediscover the problem.
    """
    httpx_logger = logging.getLogger("httpx")
    previous = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        httpx_logger.setLevel(previous)


class _HostThrottle:
    """Enforces a minimum interval between requests to the same host."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        with self._lock:
            previous = self._last.get(host)
            now = time.monotonic()
            if previous is not None:
                delay = self._min_interval - (now - previous)
                if delay > 0:
                    time.sleep(delay)
                    now = time.monotonic()
            self._last[host] = now


class PoliteClient:
    """A small httpx wrapper: shared UA, throttling, retry on transient failures."""

    def __init__(
        self,
        *,
        min_interval: float | None = None,
        timeout: float = 60.0,
        user_agent: str | None = None,
    ) -> None:
        settings = get_settings()
        self._throttle = _HostThrottle(
            min_interval if min_interval is not None else settings.twair_min_request_interval
        )
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent or settings.twair_user_agent},
        )

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type(RETRYABLE),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self._throttle.wait(url)
        log.debug("GET %s", url)
        response = self._client.get(url, **kwargs)
        # 4xx other than 429 are not worth retrying — fail fast with a clear error.
        if response.status_code == 429 or response.status_code >= 500:
            response.raise_for_status()
        response.raise_for_status()
        return response

    @retry(
        retry=retry_if_exception_type(RETRYABLE),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST with the same throttling and retry policy as :meth:`get`.

        Needed for ASP.NET WebForms endpoints (airtw), which drive their
        year selector through ``__VIEWSTATE`` postbacks rather than query
        strings. Cookies persist on the underlying client across calls.
        """
        self._throttle.wait(url)
        log.debug("POST %s", url)
        response = self._client.post(url, **kwargs)
        response.raise_for_status()
        return response

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.get(url, **kwargs).text

    def stream_to_file(self, url: str, dest: Path, **kwargs: Any) -> Path:
        """Download to ``dest`` atomically via a ``.part`` file."""
        self._throttle.wait(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        log.info("downloading %s → %s", url, dest)
        with self._client.stream("GET", url, **kwargs) as response:
            response.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
        tmp.replace(dest)
        return dest


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest() -> Iterator[dict[str, Any]]:
    """Yield every recorded download. Missing ledger yields nothing."""
    path = manifest_path()
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def manifest_lookup(key: str) -> dict[str, Any] | None:
    """Most recent ledger entry for a logical key, or None."""
    found: dict[str, Any] | None = None
    for entry in read_manifest():
        if entry.get("key") == key:
            found = entry
    return found


def record_download(
    *,
    key: str,
    url: str,
    path: Path,
    source: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a provenance record for a downloaded file."""
    entry: dict[str, Any] = {
        "key": key,
        "source": source,
        "url": url,
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if extra:
        entry.update(extra)
    ledger = manifest_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def is_cached(key: str) -> Path | None:
    """Return the cached path if a prior download is still present and intact."""
    entry = manifest_lookup(key)
    if entry is None:
        return None
    path = Path(entry["path"])
    if not path.exists():
        return None
    if path.stat().st_size != entry.get("bytes"):
        log.warning("cached file size mismatch for %s; will re-download", key)
        return None
    return path
