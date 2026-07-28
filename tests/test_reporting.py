"""Tests for report assembly.

The report's value is that it cannot drift from the results it describes, so
what matters is that every number comes from a file and that a missing file
produces an honest gap rather than an invented one.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from twair import reporting


@pytest.fixture
def outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the report's inputs to a temporary tree."""
    monkeypatch.setattr(reporting, "outputs_dir", lambda module=None: tmp_path / module)
    monkeypatch.setattr(reporting, "REPORTS_DIR", tmp_path / "reports")
    return tmp_path


def _write(root: Path, module: str, name: str, frame: pl.DataFrame) -> None:
    directory = root / module
    directory.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(directory / f"{name}.parquet")


class TestTableRendering:
    def test_nulls_render_as_a_dash_not_the_word_none(self) -> None:
        frame = pl.DataFrame({"a": [1.0, None]})

        rendered = reporting._table(frame)

        assert "—" in rendered
        assert "None" not in rendered

    def test_floats_are_rounded_consistently(self) -> None:
        frame = pl.DataFrame({"x": [1.23456789]})

        assert "1.2346" in reporting._table(frame)

    def test_long_tables_are_truncated_with_a_count(self) -> None:
        frame = pl.DataFrame({"x": list(range(100))})

        rendered = reporting._table(frame, limit=5)

        assert "共 100 列" in rendered

    def test_short_tables_carry_no_truncation_note(self) -> None:
        frame = pl.DataFrame({"x": [1, 2]})

        assert "共" not in reporting._table(frame, limit=10)


class TestGracefulDegradation:
    def test_missing_m1_is_reported_not_faked(self, outputs: Path) -> None:
        section = reporting._m1_section()

        assert "尚未執行" in section
        assert "analyze m1" in section

    def test_missing_m2_is_reported_not_faked(self, outputs: Path) -> None:
        assert "尚未執行" in reporting._m2_section()

    def test_missing_m3_is_reported_not_faked(self, outputs: Path) -> None:
        assert "尚未執行" in reporting._m3_section()

    def test_a_report_builds_with_no_inputs_at_all(self, outputs: Path) -> None:
        """A partial run must still produce a readable document."""
        path = reporting.build_core_report()

        assert path.exists()
        assert "M1" in path.read_text(encoding="utf-8")


class TestLeakPricing:
    def test_the_pm10_comparison_quantifies_the_share(self) -> None:
        rolling = pl.DataFrame(
            {
                "model": ["lightgbm", "lightgbm"],
                "feature_set": ["full", "full_with_pm10"],
                "split_kind": ["rolling", "rolling"],
                "rmse": [14.0, 9.0],
                "r2": [0.40, 0.80],
                "f1": [0.6, 0.8],
                "mae": [10.0, 7.0],
                "splits": [3, 3],
            }
        )

        note = reporting._leak_comparison(rolling)

        assert "50%" in note, "half the R² of the leaking model comes from PM10"
        assert "0.4000" in note
        assert "0.8000" in note

    def test_no_comparison_without_both_models(self) -> None:
        rolling = pl.DataFrame(
            {
                "model": ["lightgbm"],
                "feature_set": ["full"],
                "split_kind": ["rolling"],
                "rmse": [14.0],
                "r2": [0.4],
                "f1": [0.6],
                "mae": [10.0],
                "splits": [3],
            }
        )

        assert reporting._leak_comparison(rolling) == ""


class TestNumbersComeFromFiles:
    def test_sample_size_is_read_not_hardcoded(self, outputs: Path) -> None:
        _write(
            outputs,
            "m1_replication",
            "comparison",
            pl.DataFrame(
                {
                    "kind": ["sample"],
                    "item": ["N"],
                    "published_2018": [7286.0],
                    "reproduced": [1234.0],
                    "difference": [-6052.0],
                    "pct_difference": [-83.1],
                }
            ),
        )

        section = reporting._m1_section()

        assert "1,234" in section

    def test_m2_summary_aggregates_the_scores_file(self, outputs: Path) -> None:
        _write(
            outputs,
            "m2_drivers",
            "scores",
            pl.DataFrame(
                {
                    "model": ["lightgbm"] * 2,
                    "feature_set": ["full"] * 2,
                    "split_kind": ["rolling"] * 2,
                    "split": ["rolling_1", "rolling_2"],
                    "n": [100, 100],
                    "rmse": [10.0, 20.0],
                    "mae": [8.0, 16.0],
                    "r2": [0.5, 0.3],
                    "exceedance_f1": [0.6, 0.4],
                }
            ),
        )

        section = reporting._m2_section()

        assert "15.0000" in section, "the two splits average to 15"
