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
        timeout=5,
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
        "site quality trend reading map self-test passed",
        "site quality space field note self-test passed",
        "site quality trend print contract self-test passed",
        "site quality station dossier self-test passed",
        "site quality detection limitation brief self-test passed",
        "site quality health assumption-ledger self-test passed",
        "site quality forecast horizon decision self-test passed",
        "site quality methods seven-case index self-test passed",
        "site quality data provenance register self-test passed",
        "site quality compact identity self-test passed",
        "site quality sources conditional-atlas self-test passed",
        "site quality browser restart self-test passed",
        "site quality failure cleanup self-test passed",
    ]
