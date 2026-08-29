"""Exact NOAA text contracts for HYSPLIT C0."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from twair.analysis.hysplit_io import (
    MeteorologyFile,
    parse_trajectory_endpoints,
    render_trajectory_control,
    validate_complete_trajectory,
)


def _run() -> dict[str, object]:
    return {
        "arrival_utc": datetime(2025, 3, 4, 5, 30, tzinfo=UTC),
        "latitude": 25.298,
        "longitude": 121.536,
        "start_heights_m_agl": [100, 300, 500],
        "duration_hours": -72,
        "vertical_motion": 0,
        "model_top_m_agl": 10000.0,
        "meteorology_dataset": "gdas1",
    }


def _meteorology(directory: Path) -> list[MeteorologyFile]:
    return [
        MeteorologyFile(directory, "gdas1.mar25.w1", "a" * 32, "b" * 64, 600),
        MeteorologyFile(directory, "gdas1.mar25.w2", "c" * 32, "d" * 64, 700),
    ]


def test_control_renderer_follows_the_reviewed_s262_line_contract(tmp_path: Path) -> None:
    met_dir = tmp_path / "met"
    out_dir = tmp_path / "out"

    text = render_trajectory_control(
        _run(),
        _meteorology(met_dir),
        output_directory=out_dir,
        output_filename="tdump",
    )

    assert text == "\n".join(
        [
            "25 03 04 05 30",
            "3",
            "25.298000 121.536000 100",
            "25.298000 121.536000 300",
            "25.298000 121.536000 500",
            "-72",
            "0",
            "10000.0",
            "1 2",
            f"{met_dir.as_posix()}/",
            "gdas1.mar25.w1",
            f"{met_dir.as_posix()}/",
            "gdas1.mar25.w2",
            f"{out_dir.as_posix()}/",
            "tdump",
            "",
        ]
    )


@pytest.mark.parametrize(
    ("field", "changed", "message"),
    [
        ("start_heights_m_agl", [100, 500], "heights"),
        ("duration_hours", 72, "duration"),
        ("vertical_motion", 1, "vertical motion"),
        ("model_top_m_agl", 9000.0, "model top"),
        ("meteorology_dataset", "other", "meteorology dataset"),
    ],
)
def test_control_renderer_rejects_a_run_outside_the_protocol(
    tmp_path: Path,
    field: str,
    changed: object,
    message: str,
) -> None:
    run = _run()
    run[field] = changed

    with pytest.raises(ValueError, match=message):
        render_trajectory_control(
            run,
            _meteorology(tmp_path / "met"),
            output_directory=tmp_path / "out",
            output_filename="tdump",
        )


@pytest.mark.parametrize("filename", ["../gdas", "nested/gdas", r"nested\gdas"])
def test_control_renderer_rejects_filename_separators(
    tmp_path: Path,
    filename: str,
) -> None:
    files = [MeteorologyFile(tmp_path / "met", filename, "a" * 32, "b" * 64, 1)]

    with pytest.raises(ValueError, match="filename"):
        render_trajectory_control(
            _run(), files, output_directory=tmp_path / "out", output_filename="tdump"
        )


def test_control_renderer_rejects_duplicate_meteorology_names(tmp_path: Path) -> None:
    files = _meteorology(tmp_path / "met")
    files[1] = MeteorologyFile(tmp_path / "met", files[0].filename, "c" * 32, "d" * 64, 1)

    with pytest.raises(ValueError, match="duplicate"):
        render_trajectory_control(
            _run(), files, output_directory=tmp_path / "out", output_filename="tdump"
        )


@pytest.mark.parametrize("bad_path", [Path("relative"), Path("C:/work with space")])
def test_control_renderer_rejects_unsafe_external_paths(bad_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        render_trajectory_control(
            _run(),
            _meteorology(bad_path),
            output_directory=tmp_path / "out",
            output_filename="tdump",
        )


_ENDPOINT = """1 2
GDAS 25 03 01 00 00
3 BACKWARD OMEGA
25 03 04 05 25.298 121.536 100.00
25 03 04 05 25.298 121.536 300.00
25 03 04 05 25.298 121.536 500.00
2 PRESSURE THETA
1 1 25 03 04 05 30 0 0.00 25.298 121.536 100.00 1000.00 300.00
2 1 25 03 04 05 30 0 0.00 25.298 121.536 300.00 980.00 302.00
3 1 25 03 04 05 30 0 0.00 25.298 121.536 500.00 960.00 304.00
1 1 25 03 04 04 30 0 -1.00 25.250 121.400 110.00 999.00 299.00
2 1 25 03 04 04 30 0 -1.00 25.240 121.390 310.00 979.00 301.00
3 1 25 03 04 04 30 0 -1.00 25.230 121.380 510.00 959.00 303.00
1 1 25 03 04 03 30 0 -2.00 25.200 121.300 120.00 998.00 298.00
2 1 25 03 04 03 30 0 -2.00 25.190 121.290 320.00 978.00 300.00
3 1 25 03 04 03 30 0 -2.00 25.180 121.280 520.00 958.00 302.00
"""


def test_endpoint_parser_follows_s263_and_preserves_diagnostics() -> None:
    frame = parse_trajectory_endpoints(_ENDPOINT)

    assert frame["trajectory_id"].unique().sort().to_list() == [1, 2, 3]
    assert frame["meteorology_grid_id"].unique().to_list() == [1]
    assert frame["point_utc"][0] == datetime(2025, 3, 4, 5, 30, tzinfo=UTC)
    assert frame["point_utc"].dt.minute().unique().to_list() == [30]
    assert frame.filter(frame["trajectory_id"] == 1)["age_hours"].to_list() == [0, -1, -2]
    assert frame["latitude"][0] == 25.298
    assert frame["longitude"][0] == 121.536
    assert frame["height_m_agl"][0] == 100.0
    assert frame["pressure"][0] == 1000.0
    assert frame["theta"][0] == 300.0
    validate_complete_trajectory(frame, duration_hours=-2)


def test_endpoint_parser_rejects_missing_pressure_diagnostic() -> None:
    with pytest.raises(RuntimeError, match="PRESSURE"):
        parse_trajectory_endpoints(_ENDPOINT.replace("2 PRESSURE THETA", "1 THETA"))


def test_endpoint_parser_rejects_duplicate_age_rows() -> None:
    duplicate = _ENDPOINT + _ENDPOINT.splitlines()[7] + "\n"

    with pytest.raises(RuntimeError, match="duplicate"):
        parse_trajectory_endpoints(duplicate)


def test_completion_rejects_an_early_trajectory_end() -> None:
    with pytest.raises(RuntimeError, match="complete"):
        validate_complete_trajectory(
            parse_trajectory_endpoints(_ENDPOINT),
            duration_hours=-72,
        )


def test_completion_rejects_positive_age_in_a_backward_run() -> None:
    frame = parse_trajectory_endpoints(_ENDPOINT).with_columns(
        pl.when((pl.col("trajectory_id") == 1) & (pl.col("age_hours") == -1))
        .then(1.0)
        .otherwise(pl.col("age_hours"))
        .alias("age_hours")
    )

    with pytest.raises(RuntimeError, match="positive"):
        validate_complete_trajectory(frame, duration_hours=-2)


def test_endpoint_parser_rejects_non_finite_coordinates() -> None:
    with pytest.raises(RuntimeError, match="finite"):
        parse_trajectory_endpoints(
            _ENDPOINT.replace("25.298 121.536 100.00", "nan 121.536 100.00", 1)
        )


def test_endpoint_parser_rejects_trailing_malformed_rows() -> None:
    with pytest.raises(RuntimeError, match="endpoint record"):
        parse_trajectory_endpoints(_ENDPOINT + "trailing junk\n")
