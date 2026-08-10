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

import re
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


def causal_tables() -> dict[str, pl.DataFrame]:
    return {
        "effects": pl.DataFrame(
            [
                {
                    "event": "禁燒生煤",
                    "station_name": "忠明",
                    "effect": -1.5,
                    "placebo_sd": 0.4,
                    "credible": True,
                }
            ]
        )
    }


def test_m5_persists_before_optional_trends_and_reports_the_observational_contrast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 is not a precondition of M5, and used to be able to destroy it.

    `run_trend_breaks` reads `m4_deweather/monthly.parquet` and raises
    FileNotFoundError when M4 has not been run. That call sat after the whole
    event study and before `write_causal_report`, so a complete pass over every
    station — placebo controls and all — was discarded because an optional
    comparison could not start. Nothing orders the two: `run_causal` never
    touches M4's output and there is no pipeline command.
    """
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))

    from twair.analysis import causal

    monkeypatch.setattr(causal, "run_causal", lambda **_: causal_tables())

    def no_m4() -> pl.DataFrame:
        raise FileNotFoundError("m4_deweather/monthly.parquet not found — run `twair analyze m4`")

    monkeypatch.setattr(causal, "run_trend_breaks", no_m4)

    result = CliRunner().invoke(cli.app, ["analyze", "m5"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "outputs" / "m5_causal" / "effects.parquet").exists(), (
        "the event study was thrown away by a step that is not a prerequisite"
    )
    assert "skipping trend breaks" in result.output, "the skip has to be visible, not silent"
    assert "median observed-minus-predicted contrast -1.50 µg/m³" in result.output
    assert "unmarked-control-window spread (median SD) 0.40 µg/m³" in result.output
    assert re.search(r"\beffect\b", result.output.lower()) is None
    assert "median effect" not in result.output.lower()
    assert "nothing happened" not in result.output.lower()

    no_detection_tables = causal_tables()
    no_detection_tables["effects"] = no_detection_tables["effects"].with_columns(
        pl.lit(False).alias("credible")
    )
    monkeypatch.setattr(causal, "run_causal", lambda **_: no_detection_tables)

    no_detection = CliRunner().invoke(cli.app, ["analyze", "m5"])

    assert no_detection.exit_code == 0, no_detection.output
    assert "reported as not detected, not as zero" in no_detection.output.lower()
    assert re.search(r"\beffect\b", no_detection.output.lower()) is None
    assert "median effect" not in no_detection.output.lower()
    assert "shows an effect" not in no_detection.output.lower()
    assert "nothing happened" not in no_detection.output.lower()


def test_m5_still_reports_the_trend_break_when_m4_has_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The optional step is still wired up; it is only no longer fatal."""
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))

    from twair.analysis import causal

    monkeypatch.setattr(causal, "run_causal", lambda **_: causal_tables())
    monkeypatch.setattr(
        causal,
        "run_trend_breaks",
        lambda: pl.DataFrame(
            [{"event": "空污法修正", "station_name": "忠明", "delta": -0.2, "credible": False}]
        ),
    )

    result = CliRunner().invoke(cli.app, ["analyze", "m5"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "outputs" / "m5_causal" / "trend_breaks.parquet").exists()
    assert "skipping trend breaks" not in result.output
