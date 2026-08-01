"""A result that took an hour to compute must not be lost printing it.

`twair analyze m9` fitted sixteen LightGBM models, spent seven minutes doing it,
then died with

    UnicodeEncodeError: 'cp950' codec can't encode character '┆'

— rich draws its summary table with box-drawing characters, a redirected stdout
on Windows in this locale is cp950, and `write_forecast_report` came *after* the
printing. Nothing reached disk. The backtest had succeeded and the run had not.

Two fixes, and both are pinned here: the CLI reconfigures its streams to UTF-8
so the encoding cannot fail, and the analysis is persisted before it is
displayed so that no display failure of any kind can cost the computation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from twair import cli


def scores() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "horizon": 24,
                "split": f"rolling_{i}",
                "n": 1000,
                "stations": 74,
                "model_rmse": 8.0,
                "persistence_rmse": 9.0,
                "climatology_rmse": 12.0,
                "model_r2": 0.5,
                "skill_vs_persistence": 0.21,
                "skill_vs_climatology": 0.55,
                "beats_persistence": True,
            }
            for i in (1, 2)
        ]
    )


@pytest.fixture
def outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    return tmp_path / "outputs" / "m9_forecast"


def stub_backtest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the seven minutes of fitting."""
    from twair.models import forecast

    frame = scores()
    monkeypatch.setattr(
        forecast,
        "run_forecast",
        lambda **_: {"scores": frame, "by_horizon": forecast.summarise_scores(frame)},
    )


def test_the_backtest_reaches_disk_even_when_the_summary_cannot_be_printed(
    outputs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact failure, reproduced: the console raises, the parquet survives."""
    stub_backtest(monkeypatch)

    def unprintable(*_: object, **__: object) -> None:
        raise UnicodeEncodeError("cp950", "┆", 0, 1, "illegal multibyte sequence")

    monkeypatch.setattr(cli.console, "print", unprintable)

    result = CliRunner().invoke(cli.app, ["analyze", "m9"])

    assert result.exit_code != 0, "the display failure should still be reported"
    assert (outputs / "scores.parquet").exists(), "seven minutes of fitting were discarded"
    assert (outputs / "by_horizon.parquet").exists()


def test_a_successful_run_still_reports_where_it_wrote(
    outputs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the write must not move the 「wrote …」 lines away from the end."""
    stub_backtest(monkeypatch)

    result = CliRunner().invoke(cli.app, ["analyze", "m9"])

    assert result.exit_code == 0, result.output
    assert (outputs / "scores.parquet").exists()
    # Compared by position in the stream rather than by line, because rich wraps
    # a long path onto a second line and the last line is then a fragment of it.
    assert "wrote " in result.output
    assert result.output.index("wrote ") > result.output.index("skill"), (
        "the write moved, and took its report with it"
    )


def test_the_cli_talks_utf8_whatever_the_console_says() -> None:
    """The root cause. cp950 has no ┆, and rich draws every table with one."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower()
        # pytest replaces the streams with its own capture objects, which carry
        # no `reconfigure`; the import-time loop skips those the same way.
        if hasattr(stream, "reconfigure"):
            assert encoding in {"utf-8", "utf8"}, f"{stream} is {encoding}"

    assert "┆".encode() == b"\xe2\x94\x86"
