"""The reviewed test inventory exactly matches pytest collection."""

from __future__ import annotations

import subprocess
import sys

import pytest
from scripts import check_test_count
from scripts.check_test_count import mismatch, parse_collected_total


def test_a_plain_collection_summary_yields_its_total() -> None:
    assert parse_collected_total("835 tests collected in 0.57s") == 835


def test_a_selected_over_total_summary_uses_the_total() -> None:
    assert parse_collected_total("832/835 tests collected (3 deselected)") == 835


def test_output_without_a_collection_summary_fails_loudly() -> None:
    with pytest.raises(ValueError, match="collection summary"):
        parse_collected_total("collection failed before a summary")


def test_a_count_below_the_recorded_value_is_rejected() -> None:
    assert mismatch(actual=834, expected=835) is not None


def test_a_count_above_the_recorded_value_requires_the_record_to_move_too() -> None:
    assert mismatch(actual=836, expected=835) is not None


def test_an_exact_count_passes() -> None:
    assert mismatch(actual=835, expected=835) is None


def test_collection_uses_the_active_interpreter_without_project_addopts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def completed_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        called["args"] = args
        called["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, "835 tests collected in 0.57s\n", "")

    monkeypatch.setattr(subprocess, "run", completed_run)

    assert check_test_count.collect_total() == 835
    assert called["args"] == [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-o",
        "addopts=",
    ]


def test_a_failed_pytest_collection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 2, "", "private collection failure")

    monkeypatch.setattr(subprocess, "run", failed_run)

    with pytest.raises(RuntimeError, match="pytest collection failed"):
        check_test_count.collect_total()


def test_the_cli_passes_only_when_actual_and_expected_match(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_test_count,
        "load_conf",
        lambda _name: {"quality_gates": {"expected_collected_tests": 835}},
    )
    monkeypatch.setattr(check_test_count, "collect_total", lambda: 835)

    assert check_test_count.main() == 0
    stdout = capsys.readouterr().out
    assert "expected 835" in stdout
    assert "actual 835" in stdout


def test_the_cli_rejects_a_changed_inventory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_test_count,
        "load_conf",
        lambda _name: {"quality_gates": {"expected_collected_tests": 835}},
    )
    monkeypatch.setattr(check_test_count, "collect_total", lambda: 836)

    assert check_test_count.main() == 1
    assert "collected 836 tests" in capsys.readouterr().err


@pytest.mark.parametrize("expected", [0, -1, True, "835"])
def test_a_non_positive_integer_config_fails_closed(
    expected: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        check_test_count,
        "load_conf",
        lambda _name: {"quality_gates": {"expected_collected_tests": expected}},
    )

    assert check_test_count.main() == 2
    assert "could not run" in capsys.readouterr().err
