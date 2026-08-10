"""M8 Stage B measures support and association without claiming calibration.

The fixture values are contract examples. They are deliberately shaped so
stable station differences, within-station changes, and within-month spatial
differences point in different directions; one pooled coefficient therefore
cannot stand in for all three questions.
"""

from __future__ import annotations

import shutil
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import typer
from typer.testing import CliRunner

from twair import cli
from twair.analysis.satellite import (
    SatelliteAssociationResult,
    analyse_satellite_frames,
    run_satellite_association,
    satellite_analysis_dir,
    write_satellite_analysis,
)
from twair.ingest.maiac import plan_exports
from twair.ingest.maiac_import import (
    MaiacResult,
    import_exported_files,
    write_maiac_result,
)
from twair.ingest.satellite import (
    SatelliteResponse,
    SatelliteResult,
    SatelliteSource,
    acquire_s5p,
    read_satellite_result,
    write_satellite_result,
)
from twair.ingest.station_inventory import (
    maiac_generation_result_dir,
    satellite_generation_dir,
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


class CompletedLocalS5PBackend:
    def fetch(
        self,
        source: SatelliteSource,
        station_frame: pl.DataFrame,
        *,
        year: int,
        months: tuple[int, ...],
    ) -> SatelliteResponse:
        assert year == 2025
        source_offset = 100.0 if source.key == "s5p_so2" else 0.0
        rows = [
            {
                "station_name": station_name,
                "month": month,
                "value": source_offset + station_index * 10.0 + month,
            }
            for month in months
            for station_index, station_name in enumerate(station_frame["station_name"].to_list())
        ]
        return SatelliteResponse(
            rows=rows,
            image_counts=dict.fromkeys(months, 1),
            wall_seconds=0.01,
        )


def write_validated_local_inputs(
    root: Path,
    *,
    generation: bool,
) -> tuple[pl.DataFrame, str | None]:
    ground_frame, _ = complete_frames()
    ground_path = root / "processed" / "monthly" / "monthly.parquet"
    ground_path.parent.mkdir(parents=True)
    ground_frame.write_parquet(ground_path)

    names = ground_frame["station_name"].unique(maintain_order=True).to_list()
    station_frame = pl.DataFrame(
        {
            "station_name": names,
            "lon": [120.409653, 121.161933, 120.758833],
            "lat": [23.925175, 23.045083, 24.382942],
        }
    )
    months = (1, 2, 3)
    s5p = acquire_s5p(
        station_frame,
        backend=CompletedLocalS5PBackend(),
        project="test-project",
        year=2025,
        months=months,
        generated_at="2026-08-11T00:00:00+00:00",
        inventory_generation=generation,
    )
    raw_identity = s5p.manifest.get("inventory_generation_sha256")
    s5p_destination = (
        satellite_generation_dir(2025, raw_identity) if isinstance(raw_identity, str) else None
    )
    write_satellite_result(s5p, destination=s5p_destination)

    ledger = plan_exports(
        station_frame,
        project="test-project",
        year=2025,
        months=months,
        planned_at="2026-08-11T00:00:00+00:00",
        inventory_generation=generation,
    )
    source_dir = root / "maiac-csv"
    source_dir.mkdir()
    for entry in ledger.entries:
        entry.task_id = f"task-{entry.month}"
        entry.state = "COMPLETED"
        entry.submitted_at = "2026-08-11T00:01:00+00:00"
        entry.updated_at = "2026-08-11T00:02:00+00:00"
        rows = [
            f"{station_name},2025,{entry.month},{station_index * 0.1 + entry.month * 0.01},1"
            for station_index, station_name in enumerate(names)
        ]
        (source_dir / f"{entry.file_name_prefix}.csv").write_text(
            "\n".join(
                [
                    "station_name,year,month,value,source_images",
                    *rows,
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    maiac = import_exported_files(
        ledger,
        station_frame,
        source_dir=source_dir,
        months=months,
        imported_at="2026-08-11T00:03:00+00:00",
    )
    write_maiac_result(maiac)

    identity = s5p.manifest.get("inventory_generation_sha256")
    if generation:
        assert isinstance(identity, str)
        assert maiac.manifest["inventory_generation_sha256"] == identity
        return ground_frame, identity
    assert identity is None
    assert maiac.manifest.get("inventory_generation_sha256") is None
    return ground_frame, None


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


def _source_result(
    frame: pl.DataFrame,
    *,
    kind: str,
    generation: str | None = None,
) -> SatelliteResult | MaiacResult:
    manifest: dict[str, object] = {
        "schema_version": 3 if kind == "s5p" and generation else 2 if generation else 1,
        "year": 2025,
        "rows": frame.height,
        "null_values": frame["value"].null_count(),
        "station_inventory_sha256": generation or (kind[0] * 64),
    }
    if generation is not None:
        manifest["inventory_generation_sha256"] = generation
    if kind == "s5p":
        return SatelliteResult(values=frame, coverage=pl.DataFrame(), manifest=manifest)
    return MaiacResult(values=frame, coverage=pl.DataFrame(), manifest=manifest)


def test_the_runner_reads_legacy_sources_without_contacting_earth_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twair.ingest import satellite as acquisition

    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    write_validated_local_inputs(tmp_path, generation=False)
    monkeypatch.setattr(
        acquisition,
        "EarthEngineBackend",
        lambda _project: pytest.fail("the local M8 runner contacted Earth Engine"),
    )

    result = run_satellite_association(year=2025)

    assert result.manifest["mode"] == "legacy"
    assert result.manifest["sources"] == ["maiac_aod", "s5p_no2", "s5p_so2"]
    assert set(result.manifest["upstream"]) == {"ground", "maiac", "s5p"}
    assert result.manifest["upstream"]["ground"]["path"] == ("processed/monthly/monthly.parquet")
    assert len(result.manifest["upstream"]["ground"]["sha256"]) == 64
    assert result.manifest["upstream"]["s5p"]["schema_version"] == 2
    assert result.manifest["upstream"]["s5p"]["rows"] == 18
    assert len(result.manifest["upstream"]["s5p"]["manifest_sha256"]) == 64
    assert set(result.manifest["upstream"]["s5p"]["files"]) == {
        "manifest.json",
        "s5p_coverage.parquet",
        "s5p_station_month.parquet",
    }
    values_path = tmp_path / "interim" / "satellite" / "year=2025" / "s5p_station_month.parquet"
    assert result.manifest["upstream"]["s5p"]["files"]["s5p_station_month.parquet"] == (
        sha256(values_path.read_bytes()).hexdigest()
    )


def test_a_source_rewrite_during_its_validated_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twair.analysis import satellite as module

    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    write_validated_local_inputs(tmp_path, generation=False)
    actual_read = read_satellite_result

    def rewriting_read(path: Path) -> SatelliteResult:
        result = actual_read(path)
        values_path = path / "s5p_station_month.parquet"
        values_path.write_bytes(values_path.read_bytes() + b"changed-after-read")
        return result

    monkeypatch.setattr(module, "read_satellite_result", rewriting_read)

    with pytest.raises(RuntimeError, match="changed while it was being read"):
        run_satellite_association(year=2025)


def test_a_ground_rewrite_during_its_parquet_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    write_validated_local_inputs(tmp_path, generation=False)
    ground_path = tmp_path / "processed" / "monthly" / "monthly.parquet"
    actual_read = pl.read_parquet

    def rewriting_read(path: Path) -> pl.DataFrame:
        result = actual_read(path)
        if Path(path) == ground_path:
            ground_path.write_bytes(ground_path.read_bytes() + b"changed-after-read")
        return result

    monkeypatch.setattr(pl, "read_parquet", rewriting_read)

    with pytest.raises(RuntimeError, match="ground monthly table changed while it was being read"):
        run_satellite_association(year=2025)


def test_the_runner_requires_both_sources_to_match_one_requested_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twair.analysis import satellite as module

    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    generation = "b" * 64
    ground_frame, no2 = complete_frames()
    aod = satellite(
        [(row["station_name"], row["month"].month, row["value"] * 0.01) for row in no2.to_dicts()],
        source="maiac_aod",
    )
    s5p = _source_result(no2, kind="s5p", generation=generation)
    wrong_maiac = _source_result(aod, kind="maiac", generation="c" * 64)
    assert isinstance(s5p, SatelliteResult)
    assert isinstance(wrong_maiac, MaiacResult)
    for path in (
        satellite_generation_dir(2025, generation),
        maiac_generation_result_dir(2025, generation),
    ):
        path.mkdir(parents=True)
        (path / "fixture.bin").write_bytes(b"stable fixture")
    monkeypatch.setattr(module, "read_satellite_result", lambda _path: s5p)
    monkeypatch.setattr(module, "read_maiac_result", lambda _path: wrong_maiac)
    monkeypatch.setattr(pl, "read_parquet", lambda path: ground_frame)

    with pytest.raises(RuntimeError, match="requested inventory generation"):
        run_satellite_association(year=2025, generation=generation)


def test_the_runner_uses_generation_paths_for_both_validated_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    _, identity = write_validated_local_inputs(tmp_path, generation=True)
    assert isinstance(identity, str)

    result = run_satellite_association(year=2025, generation=identity)

    assert result.manifest["inventory_generation_sha256"] == identity
    assert result.manifest["upstream"]["s5p"]["path"] == (
        f"interim/satellite/generations/{identity}/year=2025"
    )
    assert result.manifest["upstream"]["maiac"]["path"] == (
        f"interim/maiac/generations/{identity}/year=2025/result"
    )


def test_generated_inputs_copied_into_legacy_paths_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    _, identity = write_validated_local_inputs(tmp_path, generation=True)
    assert isinstance(identity, str)
    shutil.copytree(
        satellite_generation_dir(2025, identity),
        tmp_path / "interim" / "satellite" / "year=2025",
    )
    shutil.copytree(
        maiac_generation_result_dir(2025, identity),
        tmp_path / "interim" / "maiac" / "year=2025" / "result",
    )

    with pytest.raises(RuntimeError, match="legacy inputs must not claim"):
        run_satellite_association(year=2025)


def test_the_cli_persists_before_explaining_the_provisional_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twair.analysis import satellite as module

    ground_frame, source_frame = complete_frames()
    result = analysed(ground_frame, source_frame)
    events: list[str] = []
    actual_print = cli.console.print

    def run(**_kwargs: object) -> SatelliteAssociationResult:
        events.append("run")
        return result

    def write(_result: SatelliteAssociationResult) -> dict[str, Path]:
        events.append("write")
        return {"manifest": tmp_path / "manifest.json"}

    def record_print(*args: object, **kwargs: Any) -> None:
        events.append("print")
        actual_print(*args, **kwargs)

    monkeypatch.setattr(module, "run_satellite_association", run)
    monkeypatch.setattr(module, "write_satellite_analysis", write)
    monkeypatch.setattr(cli.console, "print", record_print)

    response = CliRunner().invoke(cli.app, ["analyze", "m8", "--year", "2025"])

    assert response.exit_code == 0, response.output
    assert events[:3] == ["run", "write", "print"]
    assert "provisional descriptive association" in response.output
    assert "not calibration" in response.output
    assert "input identity: year 2025; mode legacy" in response.output
    assert "within_station" in response.output
    assert "wrote manifest" in response.output


def test_the_cli_rejects_a_bad_generation_before_reading_any_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from twair.analysis import satellite as module

    monkeypatch.setattr(
        module,
        "run_satellite_association",
        lambda **_kwargs: pytest.fail("source loading began before generation validation"),
    )

    with pytest.raises(typer.BadParameter, match="full 64-character lowercase SHA-256"):
        cli.analyze_m8(year=2025, generation="short")
