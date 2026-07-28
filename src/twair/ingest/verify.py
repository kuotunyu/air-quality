"""Live credential verification.

Checking that a variable is *set* is nearly worthless — a typo, a trailing
space, or a key that was never activated all look identical to a presence
check. Each provider is therefore contacted with the cheapest request that
proves the credential works.

Nothing here ever prints a key. Failures report the provider's own message so
the cause is actionable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from twair.config import get_settings
from twair.net import quiet_http as _quiet_http

log = logging.getLogger(__name__)

__all__ = ["CheckResult", "verify_all"]

TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    env_var: str
    purpose: str
    configured: bool
    working: bool | None
    """None when the credential is absent, so no check was attempted."""
    detail: str = ""

    @property
    def status(self) -> str:
        if not self.configured:
            return "missing"
        if self.working is None:
            return "unchecked"
        return "ok" if self.working else "failed"


def _moenv(key: str) -> tuple[bool, str]:
    url = "https://data.moenv.gov.tw/api/v2/aqx_p_432"
    try:
        response = httpx.get(
            url, params={"limit": 1, "api_key": key, "format": "json"}, timeout=TIMEOUT
        )
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"

    if response.status_code == 401:
        return False, "401 — key rejected"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"

    try:
        payload = response.json()
    except ValueError:
        return False, "response was not JSON (often an error page)"

    # The platform is inconsistent: some datasets wrap results in an object
    # with a `records` key, others return the record list directly.
    if isinstance(payload, list):
        return True, f"{len(payload)} record(s) returned"
    if isinstance(payload, dict):
        records = payload.get("records")
        if records is not None:
            return True, f"{len(records)} record(s) returned"
        if payload.get("message") or payload.get("error"):
            return False, str(payload.get("message") or payload.get("error"))[:120]
        return False, f"unexpected shape; keys={list(payload)[:5]}"
    return False, f"unexpected response type {type(payload).__name__}"


def _cwa(key: str) -> tuple[bool, str]:
    # O-A0003-001 = 現在天氣觀測報告 (automatic weather stations).
    url = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001"
    try:
        response = httpx.get(
            url, params={"Authorization": key, "limit": 1, "format": "JSON"}, timeout=TIMEOUT
        )
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"

    if response.status_code == 401:
        return False, "401 — authorisation code rejected"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"

    try:
        payload = response.json()
    except ValueError:
        return False, "response was not JSON"

    if payload.get("success") in {"true", True}:
        stations = payload.get("records", {}).get("Station", [])
        return True, f"{len(stations)} station record(s) returned"
    return False, f"success={payload.get('success')!r}"


def _huggingface(token: str) -> tuple[bool, str]:
    try:
        response = httpx.get(
            "https://huggingface.co/api/whoami-v2",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"

    if response.status_code == 401:
        return False, "401 — token rejected"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"

    payload = response.json()
    name = payload.get("name", "?")
    scopes = payload.get("auth", {}).get("accessToken", {}).get("role", "?")
    if scopes not in {"write", "admin", "fineGrained"}:
        return False, f"user {name}, but role is {scopes!r} — publishing needs write"
    return True, f"user {name}, role {scopes}"


def _cds(key: str) -> tuple[bool, str]:
    settings = get_settings()
    url = settings.cdsapi_url.rstrip("/")
    try:
        # The account endpoint 307-redirects; httpx does not follow by default.
        response = httpx.get(
            f"{url}/profiles/v1/account",
            headers={"PRIVATE-TOKEN": key},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"

    if response.status_code in {401, 403}:
        return False, f"HTTP {response.status_code} — token rejected"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"

    payload = response.json()
    user = payload.get("uid") or payload.get("user_id") or "?"
    return True, (
        f"account {user}. Note: each dataset's licence must also be accepted "
        f"on the CDS website before download."
    )


def _earthengine(project: str) -> tuple[bool, str]:
    """Local check only — GEE needs an interactive OAuth flow to authenticate."""
    try:
        import ee
    except ImportError:
        return False, "earthengine-api not installed (uv sync --extra earth)"

    credentials = Path.home() / ".config" / "earthengine" / "credentials"
    if not credentials.exists():
        return (
            False,
            f"project set but not authenticated — run `earthengine authenticate` ({project})",
        )

    try:
        import ee

        ee.Initialize(project=project)
        count = ee.ImageCollection("COPERNICUS/S5P/OFFL/L3_NO2").limit(1).size().getInfo()
    except Exception as exc:
        return False, f"initialise failed: {exc}"

    return True, f"project {project}, Sentinel-5P reachable ({count} image sampled)"


CHECKS = (
    (
        "MOENV open data",
        "MOENV_API_KEY",
        "moenv_api_key",
        "Station metadata, daily updates",
        _moenv,
    ),
    ("CWA open data", "CWA_API_KEY", "cwa_api_key", "Weather station observations", _cwa),
    ("Copernicus CDS", "CDSAPI_KEY", "cdsapi_key", "ERA5 boundary layer height", _cds),
    ("Earth Engine", "GEE_PROJECT_ID", "gee_project_id", "Sentinel-5P, MODIS AOD", _earthengine),
    ("HuggingFace", "HF_TOKEN", "hf_token", "Dataset and Space publishing", _huggingface),
)


def verify_all(*, live: bool = True) -> list[CheckResult]:
    """Check every credential, contacting the provider unless ``live`` is off."""
    settings = get_settings()
    results: list[CheckResult] = []

    for name, env_var, field, purpose, checker in CHECKS:
        value = getattr(settings, field, None)
        if not value:
            results.append(CheckResult(name, env_var, purpose, False, None))
            continue
        if not live:
            results.append(CheckResult(name, env_var, purpose, True, None, "not checked"))
            continue

        log.debug("verifying %s", name)
        try:
            with _quiet_http():
                working, detail = checker(str(value))
        except Exception as exc:
            working, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(CheckResult(name, env_var, purpose, True, working, detail))

    return results
