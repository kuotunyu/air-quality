"""The credential checks, checked.

`twair/ingest/verify.py` opens by saying that a presence check is nearly
worthless — "a typo, a trailing space, or a key that was never activated all
look identical" — and then it was itself the least verified module in the
package: 132 statements at 0% coverage, every branch of every provider's
response handling unexercised.

That matters more than the number suggests. These functions exist to turn a
provider's answer into a yes or a no, and the answers are inconsistent by
design: MOENV wraps results in `records` for some datasets and returns a bare
list for others, CWA reports success as the STRING "true" as well as the
boolean, and an expired token comes back as a 200 with an error body rather
than a 401. Every one of those is a branch here, and a wrong branch reports a
broken credential as working — which is the one failure this file exists to
prevent.

`respx` has been a declared dev dependency with no callers since it was added.
This is what it was for: the provider is mocked at the transport layer, so the
tests exercise the real `httpx` call path without touching the network.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from twair.ingest.verify import (
    CheckResult,
    _cds,
    _cwa,
    _earthengine,
    _huggingface,
    _moenv,
    verify_all,
)

MOENV_URL = "https://data.moenv.gov.tw/api/v2/aqx_p_432"
CWA_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0003-001"
HF_URL = "https://huggingface.co/api/whoami-v2"


# ── MOENV ────────────────────────────────────────────────────────────────────


@respx.mock
def test_moenv_accepts_a_bare_list() -> None:
    """Some datasets return the records directly rather than wrapped."""
    respx.get(MOENV_URL).mock(return_value=httpx.Response(200, json=[{"a": 1}, {"a": 2}]))
    ok, detail = _moenv("KEY")
    assert ok
    assert "2 record(s)" in detail


@respx.mock
def test_moenv_accepts_the_wrapped_shape() -> None:
    respx.get(MOENV_URL).mock(return_value=httpx.Response(200, json={"records": [{"a": 1}]}))
    ok, detail = _moenv("KEY")
    assert ok
    assert "1 record(s)" in detail


@respx.mock
def test_moenv_rejects_a_rejected_key() -> None:
    respx.get(MOENV_URL).mock(return_value=httpx.Response(401))
    ok, detail = _moenv("KEY")
    assert not ok
    assert "401" in detail


@respx.mock
def test_moenv_rejects_an_error_body_that_arrived_with_a_200() -> None:
    """The failure mode the docstring is about: a 200 that is not a success."""
    respx.get(MOENV_URL).mock(
        return_value=httpx.Response(200, json={"message": "API key not activated"})
    )
    ok, detail = _moenv("KEY")
    assert not ok
    assert "not activated" in detail


@respx.mock
def test_moenv_rejects_an_html_error_page() -> None:
    respx.get(MOENV_URL).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    ok, detail = _moenv("KEY")
    assert not ok
    assert "not JSON" in detail


@respx.mock
def test_moenv_rejects_a_shape_it_does_not_recognise() -> None:
    """Neither a list nor a dict carrying `records` or an error."""
    respx.get(MOENV_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    ok, detail = _moenv("KEY")
    assert not ok
    assert "unexpected shape" in detail


@respx.mock
def test_moenv_reports_a_network_error_rather_than_raising() -> None:
    respx.get(MOENV_URL).mock(side_effect=httpx.ConnectError("no route to host"))
    ok, detail = _moenv("KEY")
    assert not ok
    assert "network error" in detail


@respx.mock
def test_moenv_never_puts_the_key_in_the_message() -> None:
    """`Nothing here ever prints a key` — the module's own promise."""
    secret = "SUPERSECRET-KEY-0123456789"
    respx.get(MOENV_URL).mock(return_value=httpx.Response(500))
    ok, detail = _moenv(secret)
    assert not ok
    assert secret not in detail


# ── CWA ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("success", ["true", True])
@respx.mock
def test_cwa_accepts_both_spellings_of_success(success: str | bool) -> None:
    """CWA reports success as the string "true" as well as the boolean."""
    respx.get(CWA_URL).mock(
        return_value=httpx.Response(
            200, json={"success": success, "records": {"Station": [{"s": 1}, {"s": 2}]}}
        )
    )
    ok, detail = _cwa("KEY")
    assert ok
    assert "2 station record(s)" in detail


@respx.mock
def test_cwa_rejects_success_false() -> None:
    respx.get(CWA_URL).mock(return_value=httpx.Response(200, json={"success": "false"}))
    ok, _ = _cwa("KEY")
    assert not ok


@respx.mock
def test_cwa_rejects_a_rejected_authorisation_code() -> None:
    respx.get(CWA_URL).mock(return_value=httpx.Response(401))
    ok, detail = _cwa("KEY")
    assert not ok
    assert "401" in detail


# ── HuggingFace ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["write", "admin", "fineGrained"])
@respx.mock
def test_huggingface_accepts_a_role_that_can_publish(role: str) -> None:
    respx.get(HF_URL).mock(
        return_value=httpx.Response(
            200, json={"name": "someone", "auth": {"accessToken": {"role": role}}}
        )
    )
    ok, detail = _huggingface("TOKEN")
    assert ok
    assert role in detail


@respx.mock
def test_huggingface_rejects_a_read_only_token() -> None:
    """A valid token that cannot do the job is not a working credential.

    This is the case a presence check gets wrong every time: the variable is
    set, the request succeeds, and publishing would still fail.
    """
    respx.get(HF_URL).mock(
        return_value=httpx.Response(
            200, json={"name": "someone", "auth": {"accessToken": {"role": "read"}}}
        )
    )
    ok, detail = _huggingface("TOKEN")
    assert not ok
    assert "read" in detail
    assert "write" in detail


# ── the roll-up ──────────────────────────────────────────────────────────────


def test_verify_all_without_live_contacts_nobody() -> None:
    """`live=False` must not open a socket. respx asserts that for us."""
    with respx.mock(assert_all_called=False) as router:
        results = verify_all(live=False)
        assert not router.calls, "verify_all(live=False) made a request"

    assert results, "no credentials are declared at all"
    for r in results:
        # Either absent, or present-but-unchecked. Never a verdict.
        assert r.working is None
        if r.configured:
            assert r.detail == "not checked"


def test_a_missing_credential_is_not_reported_as_broken() -> None:
    """`working is None` is the third state, and it has to stay distinct.

    Collapsing it into False would report every unconfigured optional provider
    as a failure, which is how a status display stops being read.
    """
    absent = CheckResult("X", "X_KEY", "purpose", configured=False, working=None)
    broken = CheckResult("Y", "Y_KEY", "purpose", configured=True, working=False, detail="401")

    assert absent.working is None
    assert broken.working is False
    assert absent.working is not broken.working


# ── Copernicus CDS ───────────────────────────────────────────────────────────


@respx.mock
def test_cds_accepts_an_account_and_still_warns_about_licences() -> None:
    """Reachable is not the same as usable, and the detail has to say so.

    A CDS token can authenticate perfectly and every download still fail,
    because each dataset's licence has to be accepted separately on the
    website. A check that reported only "ok" would send someone to debug their
    key.
    """
    from twair.config import get_settings

    url = get_settings().cdsapi_url.rstrip("/")
    respx.get(f"{url}/profiles/v1/account").mock(
        return_value=httpx.Response(200, json={"uid": 4242})
    )
    ok, detail = _cds("TOKEN")
    assert ok
    assert "4242" in detail
    assert "licence" in detail


@pytest.mark.parametrize("code", [401, 403])
@respx.mock
def test_cds_rejects_a_rejected_token(code: int) -> None:
    from twair.config import get_settings

    url = get_settings().cdsapi_url.rstrip("/")
    respx.get(f"{url}/profiles/v1/account").mock(return_value=httpx.Response(code))
    ok, detail = _cds("TOKEN")
    assert not ok
    assert str(code) in detail


# ── Earth Engine ─────────────────────────────────────────────────────────────


def test_earthengine_says_which_step_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not installed and not authenticated are different problems.

    Both leave GEE unusable, and they need different actions — one is a
    `uv sync`, the other an interactive OAuth flow. Reporting either as "failed"
    would make the message useless, which is what this module exists to avoid.
    """
    import builtins

    real_import = builtins.__import__

    def no_ee(name: str, *args: object, **kwargs: object) -> object:
        if name == "ee":
            raise ImportError("no module named ee")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", no_ee)
    ok, detail = _earthengine("some-project")
    assert not ok
    assert "not installed" in detail


# ── CheckResult.status ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("configured", "working", "expected"),
    [
        (False, None, "missing"),
        (True, None, "unchecked"),
        (True, True, "ok"),
        (True, False, "failed"),
    ],
)
def test_status_has_four_distinct_words(
    configured: bool, working: bool | None, expected: str
) -> None:
    """Four states, and the display collapses to three if any two are confused.

    `missing` and `failed` are the pair that matters: an optional credential
    nobody set is not a broken one, and a status list that says otherwise
    trains its reader to ignore it.
    """
    result = CheckResult("N", "N_KEY", "purpose", configured=configured, working=working)
    assert result.status == expected


@respx.mock
def test_verify_all_live_reports_each_provider_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One configured key, and only that provider is contacted.

    Also covers the shape of the roll-up: an absent credential stays
    `working is None` even on a live run, so a single configured provider
    cannot make the others look checked.
    """
    import twair.ingest.verify as v

    class Settings:
        moenv_api_key = "KEY"
        cwa_api_key = None
        cdsapi_key = None
        gee_project_id = None
        hf_token = None
        cdsapi_url = "https://cds.example/api"

    monkeypatch.setattr(v, "get_settings", Settings)
    respx.get(MOENV_URL).mock(return_value=httpx.Response(200, json={"records": [{"a": 1}]}))

    results = {r.env_var: r for r in v.verify_all(live=True)}

    assert results["MOENV_API_KEY"].status == "ok"
    assert results["CWA_API_KEY"].status == "missing"
    assert results["HF_TOKEN"].working is None
    assert len(respx.calls) == 1, "a provider with no credential was contacted"


def test_a_checker_that_raises_becomes_a_failed_check_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`verify_all` is a status display; one broken provider must not end it.

    The handler turns any exception into a failed result carrying the type
    name, so an unexpected error from one provider still leaves the other four
    reported.
    """
    import twair.ingest.verify as v

    class Settings:
        moenv_api_key = "KEY"
        cwa_api_key = None
        cdsapi_key = None
        gee_project_id = None
        hf_token = None
        cdsapi_url = "https://cds.example/api"

    def explode(_key: str) -> tuple[bool, str]:
        raise RuntimeError("provider changed its API")

    monkeypatch.setattr(v, "get_settings", Settings)
    monkeypatch.setattr(
        v,
        "CHECKS",
        (("MOENV open data", "MOENV_API_KEY", "moenv_api_key", "purpose", explode),),
    )

    results = v.verify_all(live=True)

    assert len(results) == 1
    assert results[0].status == "failed"
    assert "RuntimeError" in results[0].detail
    assert "provider changed its API" in results[0].detail
