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
            "m1_baseline",
            "panel",
            pl.DataFrame(
                {
                    "station_name": ["二林"] * 1234,
                    "PM2.5": [20.0] * 1234,
                }
            ),
        )
        _write(
            outputs,
            "m1_baseline",
            "correlations",
            pl.DataFrame({"variable": ["PM10"], "r": [0.889]}),
        )
        _write(
            outputs,
            "m1_baseline",
            "ols",
            pl.DataFrame(
                {
                    "term": ["PM10"],
                    "coefficient": [0.402],
                    "std_error": [0.004],
                    "t": [86.28],
                    "p": [0.0],
                    "vif": [12345.0],
                    "r_squared": [0.8215],
                }
            ),
        )

        section = reporting._m1_section()

        assert "1,234" in section, "the sample size must come from the panel file"

    def test_the_spatial_moran_table_is_read_not_hardcoded(self, outputs: Path) -> None:
        _write(
            outputs,
            "m6_spatial",
            "partition_price",
            pl.DataFrame(
                {
                    "control": ["pooled", "within_zone_separate_fits"],
                    "design_columns": [13, 104],
                    "r_squared": [0.8215, 0.8695],
                    "mean_i": [0.4242, 0.0111],
                    "mean_i_lo": [0.4, 0.0],
                    "mean_i_hi": [0.45, 0.02],
                    "months_scored": [96, 96],
                    "months_significant_bh": [55, 9],
                }
            ),
        )

        section = reporting._spatial_partition_section()

        assert "+0.424" in section, "the pooled mean I must come from the file"
        assert "+0.011" in section
        assert "55/96" in section

    def test_the_terms_the_two_way_correction_costs_are_derived(self, outputs: Path) -> None:
        """The list of terms losing significance is a query, not a memory."""
        _write(
            outputs,
            "m6_spatial",
            "inference_price",
            pl.DataFrame(
                {
                    "term": ["PM10", "RAINFALL", "PM10", "RAINFALL"],
                    "cov_type": ["iid", "iid", "cluster_twoway", "cluster_twoway"],
                    "coefficient": [0.4, -0.5, 0.4, -0.5],
                    "se": [0.005, 0.17, 0.03, 0.37],
                    "t": [86.28, -2.77, 14.07, -1.29],
                    "p": [0.0, 0.005, 0.0, 0.196],
                    "se_inflation_vs_iid": [1.0, 1.0, 6.13, 2.14],
                    "psd_fix_applied": [False, False, False, False],
                }
            ),
        )

        section = reporting._spatial_inference_section()

        assert "86.28" in section and "14.07" in section
        assert "失去顯著：RAINFALL" in section, "RAINFALL crosses 0.05, PM10 does not"
        assert "PM10、" not in section.split("失去顯著：")[1]

    def test_a_correlogram_that_never_changes_sign_is_not_called_a_dipole(
        self, outputs: Path
    ) -> None:
        _write(
            outputs,
            "m6_spatial",
            "correlogram",
            pl.DataFrame(
                {
                    "bin_lo_km": [0.0, 100.0],
                    "bin_hi_km": [10.0, 150.0],
                    "i": [0.348, 0.104],
                    "z": [2.46, 1.10],
                    "significant_bh": [True, False],
                }
            ),
        )

        section = reporting._spatial_distance_section()

        assert "降至" in section
        assert "反號至" not in section

    def test_the_bands_surviving_correction_are_named_not_left_to_a_z_value(
        self, outputs: Path
    ) -> None:
        """Restoring one station to M6's network moved two near bands from
        significant to not, without changing their z much. Printing z alone
        would have hidden that, so the report names the survivors."""
        _write(
            outputs,
            "m6_spatial",
            "correlogram",
            pl.DataFrame(
                {
                    "bin_lo_km": [0.0, 30.0, 100.0],
                    "bin_hi_km": [10.0, 50.0, 150.0],
                    "i": [0.277, 0.237, -0.230],
                    "z": [1.98, 3.06, -4.55],
                    "significant_bh": [False, True, True],
                }
            ),
        )

        section = reporting._spatial_distance_section()

        assert "2/3 個" in section
        assert "30–50 km" in section and "100–150 km" in section
        assert "0–10 km、" not in section, "a band that failed correction is not a survivor"

    def test_missing_m6_is_reported_not_faked(self, outputs: Path) -> None:
        report = reporting.build_spatial_report()

        text = report.read_text(encoding="utf-8")
        assert "尚未產出" in text
        assert "analyze m6" in text

    def test_the_header_block_survives_interpolation(self, outputs: Path) -> None:
        """A multi-line metadata block must not break out of the blockquote."""
        _write(
            outputs,
            "m6_spatial",
            "metadata",
            pl.DataFrame(
                {
                    "key": ["seed", "residual_null_draws", "weights", "panel_stations"],
                    "value": ["12345", "999", "knn(5)", "72"],
                }
            ),
        )

        text = reporting.build_spatial_report().read_text(encoding="utf-8")

        block = [line for line in text.splitlines() if "12345" in line]
        assert block and all(line.startswith(">") for line in block)

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
