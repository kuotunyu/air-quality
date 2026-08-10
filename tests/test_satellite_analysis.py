"""M8 Stage B measures support and association without claiming calibration.

The fixture values are contract examples. They are deliberately shaped so
stable station differences, within-station changes, and within-month spatial
differences point in different directions; one pooled coefficient therefore
cannot stand in for all three questions.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from twair.analysis.satellite import (
    SatelliteAssociationResult,
    analyse_satellite_frames,
    satellite_analysis_dir,
    write_satellite_analysis,
)


def ground(rows: list[tuple[str, int, float | None, bool]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": [row[0] for row in rows],
            "pollutant": ["PM2.5"] * len(rows),
            "month": [date(2025, row[1], 1) for row in rows],
            "mean": [row[2] for row in rows],
            "meets_threshold": [row[3] for row in rows],
        },
        schema={
            "station_name": pl.String,
            "pollutant": pl.String,
            "month": pl.Date,
            "mean": pl.Float64,
            "meets_threshold": pl.Boolean,
        },
    )


def satellite(
    rows: list[tuple[str, int, float | None]], *, source: str = "s5p_no2"
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": [row[0] for row in rows],
            "month": [date(2025, row[1], 1) for row in rows],
            "source": [source] * len(rows),
            "value": [row[2] for row in rows],
            "unit": ["mol/m^2"] * len(rows),
            "collection_id": ["collection"] * len(rows),
            "band": ["band"] * len(rows),
            "sample_scale_m": [1113] * len(rows),
        },
        schema={
            "station_name": pl.String,
            "month": pl.Date,
            "source": pl.String,
            "value": pl.Float64,
            "unit": pl.String,
            "collection_id": pl.String,
            "band": pl.String,
            "sample_scale_m": pl.Int32,
        },
    )


def analysed(
    ground_frame: pl.DataFrame,
    satellite_frame: pl.DataFrame,
    *,
    generation: str | None = None,
) -> SatelliteAssociationResult:
    return analyse_satellite_frames(
        ground_frame,
        satellite_frame,
        year=2025,
        generation=generation,
        upstream={"fixture": {"manifest_sha256": "f" * 64}},
        generated_at="2026-08-11T00:00:00+00:00",
        git_sha="a" * 40,
        git_dirty=False,
    )


def complete_frames() -> tuple[pl.DataFrame, pl.DataFrame]:
    ground_rows: list[tuple[str, int, float | None, bool]] = []
    satellite_rows: list[tuple[str, int, float | None]] = []
    for station, source_base, ground_base in (
        ("甲", 0.0, 10.0),
        ("乙", 100.0, 110.0),
        ("丙", 200.0, 210.0),
    ):
        for month, source_change, ground_change in (
            (1, 1.0, 0.0),
            (2, 2.0, -1.0),
            (3, 3.0, -2.0),
        ):
            satellite_rows.append((station, month, source_base + source_change))
            ground_rows.append((station, month, ground_base + ground_change, True))
    return ground(ground_rows), satellite(satellite_rows)


def test_coverage_distinguishes_source_null_ground_withheld_and_ground_absent() -> None:
    ground_frame = ground(
        [
            ("甲", 1, 10.0, True),
            ("甲", 2, 11.0, True),
            ("甲", 3, None, False),
            ("乙", 1, 20.0, True),
            ("乙", 2, 21.0, True),
        ]
    )
    source_frame = satellite(
        [
            ("甲", 1, 1.0),
            ("甲", 2, None),
            ("甲", 3, 3.0),
            ("乙", 1, 4.0),
            ("乙", 2, 5.0),
            ("乙", 3, 6.0),
        ]
    )

    result = analysed(ground_frame, source_frame)
    row = result.coverage.row(0, named=True)

    assert row == {
        "source": "s5p_no2",
        "source_rows": 6,
        "satellite_observed_rows": 5,
        "satellite_null_rows": 1,
        "ground_present_rows": 5,
        "ground_absent_rows": 1,
        "ground_observed_rows": 4,
        "ground_withheld_rows": 1,
        "paired_rows": 3,
        "source_stations": 2,
        "paired_stations": 2,
        "source_months": 3,
        "paired_months": 2,
        "paired_fraction": 0.5,
    }
    assert result.panel.filter(pl.col("satellite_value").is_null()).height == 1
    assert result.panel.filter(pl.col("ground_withheld")).height == 1
    assert result.panel.filter(~pl.col("ground_row_present")).height == 1
    assert result.panel["satellite_value"].null_count() == 1
    assert result.panel["ground_value"].null_count() == 2


def test_the_three_association_scopes_answer_different_questions() -> None:
    ground_frame, source_frame = complete_frames()

    rows = {
        row["scope"]: row
        for row in analysed(ground_frame, source_frame).association.iter_rows(named=True)
    }

    assert rows["pooled"]["pearson_r"] > 0.99
    assert rows["within_station"]["pearson_r"] == pytest.approx(-1.0)
    assert rows["within_month"]["pearson_r"] == pytest.approx(1.0)
    assert all(row["n_pairs"] == 9 for row in rows.values())
    assert all(row["n_stations"] == 3 for row in rows.values())
    assert all(row["n_months"] == 3 for row in rows.values())


def test_station_and_month_context_keep_support_beside_each_correlation() -> None:
    ground_frame, source_frame = complete_frames()

    result = analysed(ground_frame, source_frame)

    assert result.station_context.height == 3
    assert result.month_context.height == 3
    assert result.station_context["n_pairs"].to_list() == [3, 3, 3]
    assert result.station_context["pearson_r"].to_list() == pytest.approx([-1.0] * 3)
    assert result.month_context["n_pairs"].to_list() == [3, 3, 3]
    assert result.month_context["pearson_r"].to_list() == pytest.approx([1.0] * 3)


def test_too_few_pairs_and_constant_values_are_refused_as_null_not_zero() -> None:
    too_few = analysed(
        ground([("甲", 1, 10.0, True), ("甲", 2, 11.0, True)]),
        satellite([("甲", 1, 1.0), ("甲", 2, 2.0)]),
    ).association
    constant = analysed(
        ground([("甲", 1, 10.0, True), ("甲", 2, 11.0, True), ("甲", 3, 12.0, True)]),
        satellite([("甲", 1, 1.0), ("甲", 2, 1.0), ("甲", 3, 1.0)]),
    ).association

    assert too_few["pearson_r"].null_count() == too_few.height
    assert set(too_few["refusal"].drop_nulls()) == {"fewer_than_three_pairs"}
    assert constant["pearson_r"].null_count() == constant.height
    assert "constant_satellite_value" in set(constant["refusal"].drop_nulls())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate_satellite", "duplicate source-station-month"),
        ("nonfinite_satellite", "satellite values must be finite or null"),
        ("duplicate_ground", "duplicate PM2.5 station-month"),
        ("inconsistent_ground_null", "withheld PM2.5 means must be null"),
    ],
)
def test_invalid_input_cannot_become_an_association(mutation: str, message: str) -> None:
    ground_frame, source_frame = complete_frames()
    if mutation == "duplicate_satellite":
        source_frame = pl.concat([source_frame, source_frame.head(1)])
    elif mutation == "nonfinite_satellite":
        source_frame = source_frame.with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(float("inf"))
            .otherwise(pl.col("value"))
            .alias("value")
        )
    elif mutation == "duplicate_ground":
        ground_frame = pl.concat([ground_frame, ground_frame.head(1)])
    else:
        ground_frame = ground_frame.with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(False)
            .otherwise(pl.col("meets_threshold"))
            .alias("meets_threshold")
        )

    with pytest.raises(RuntimeError, match=message):
        analysed(ground_frame, source_frame)


def test_different_units_are_kept_separate_and_never_turned_into_a_bias() -> None:
    ground_frame, source_frame = complete_frames()

    result = analysed(ground_frame, source_frame)

    assert result.panel["satellite_unit"].unique().to_list() == ["mol/m^2"]
    assert result.panel["ground_unit"].unique().to_list() == ["ug/m3"]
    for frame in (
        result.panel,
        result.coverage,
        result.association,
        result.station_context,
        result.month_context,
    ):
        assert not any(token in column for column in frame.columns for token in ("bias", "delta"))


def test_association_tables_do_not_publish_naive_independence_p_values() -> None:
    ground_frame, source_frame = complete_frames()
    result = analysed(ground_frame, source_frame)

    assert not any("p_value" in column or column == "p" for column in result.association.columns)
    assert "not causal" in result.manifest["claim_boundary"]
    assert "not calibration" in result.manifest["claim_boundary"]


def test_legacy_and_generation_results_have_disjoint_output_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    generation = "b" * 64

    assert satellite_analysis_dir(2025) == (
        tmp_path / "outputs" / "m8_satellite" / "legacy" / "year=2025"
    )
    assert satellite_analysis_dir(2025, generation) == (
        tmp_path / "outputs" / "m8_satellite" / "generations" / generation / "year=2025"
    )


@pytest.mark.parametrize("generation", ["short", "A" * 64, "g" * 64])
def test_only_a_full_lowercase_sha256_can_select_an_analysis_generation(
    generation: str,
) -> None:
    with pytest.raises(ValueError, match=r"full .*lowercase SHA-256"):
        satellite_analysis_dir(2025, generation)


def test_the_writer_persists_a_complete_result_and_binds_its_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    ground_frame, source_frame = complete_frames()
    result = analysed(ground_frame, source_frame)

    paths = write_satellite_analysis(result)

    assert set(paths) == {
        "panel",
        "coverage",
        "association",
        "station_context",
        "month_context",
        "manifest",
    }
    assert all(path.exists() for path in paths.values())
    assert pl.read_parquet(paths["panel"]).equals(result.panel)
    assert result.manifest["table_rows"] == {
        "panel": 9,
        "coverage": 1,
        "association": 3,
        "station_context": 3,
        "month_context": 3,
    }


def test_a_failed_analysis_rewrite_preserves_the_previous_complete_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    ground_frame, source_frame = complete_frames()
    result = analysed(ground_frame, source_frame)
    paths = write_satellite_analysis(result)
    before = {name: path.read_bytes() for name, path in paths.items()}

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected analysis write failure")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", fail_write)
    with pytest.raises(OSError, match="injected analysis write failure"):
        write_satellite_analysis(result)

    assert {name: path.read_bytes() for name, path in paths.items()} == before
    assert not list(paths["manifest"].parent.parent.glob(".year=2025.staging-*"))
