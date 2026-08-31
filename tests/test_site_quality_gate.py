"""The browser publication gate must fail promptly when Chrome stops replying."""

from __future__ import annotations

import subprocess

from twair.paths import REPO_ROOT


def test_the_site_quality_gate_self_tests_its_bounded_browser_protocol() -> None:
    result = subprocess.run(
        [
            "node",
            str(REPO_ROOT / "scripts" / "check_site_quality.mjs"),
            "--self-test",
            "--dist",
            str(REPO_ROOT / "path-that-does-not-exist"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        check=False,
        # 60s is a hang detector, not a speed assertion. The self-test spawns
        # node against a path that does not exist and returns in 0.24s measured
        # locally over three runs, so five seconds looked like a 20x margin —
        # and CI still blew it on 7262edf, a commit whose entire diff was
        # `<strong>` tags in one `.astro` file, which this test cannot reach
        # because it never reads the built site.
        #
        # Same shape as the CDP timeout `ci.yml` raised from 15s to 45s for the
        # same reason: a shared runner's cold start is not a property of the
        # code. A real hang is unbounded, so a slower ceiling costs a minute
        # once and stops training people to re-run red builds without reading
        # them. Every other subprocess timeout in this repository is 10, 30,
        # 120 or 300; five was the outlier.
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "site quality browser lifecycle self-test passed",
        "site quality browser startup self-test passed",
        "site quality no-JavaScript navigation self-test passed",
        "site quality render wait self-test passed",
        "site quality homepage first-viewport self-test passed",
        "site quality homepage editorial layout self-test passed",
        "site quality homepage editorial order self-test passed",
        "site quality homepage mobile type self-test passed",
        "site quality chapter opening self-test passed",
        "site quality chapter ending self-test passed",
        "site quality trend reading map self-test passed",
        "site quality space field note self-test passed",
        "site quality trend print contract self-test passed",
        "site quality station dossier self-test passed",
        "site quality station locator self-test passed",
        "site quality detection limitation brief self-test passed",
        "site quality health assumption-ledger self-test passed",
        "site quality forecast horizon decision self-test passed",
        "site quality methods seven-case index self-test passed",
        "site quality data provenance register self-test passed",
        "site quality explore guided local workspace self-test passed",
        "site quality concept diagrams self-test passed",
        "site quality compact identity self-test passed",
        "site quality sources conditional-atlas self-test passed",
        "site quality public operational copy self-test passed",
        "site quality browser restart self-test passed",
        "site quality failure cleanup self-test passed",
    ]
