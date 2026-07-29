"""Tests for M7 — CBPF source identification.

Built on synthetic stations whose source geometry is known by construction: one
with a nearby source that only shows up when the wind is weak, one with a
distant source that needs wind to arrive, and one with nothing at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest

from twair.analysis import sources
from twair.analysis.sources import cbpf, directional_concentration


def _within_one_sector(peak: int | None, bearing: float, width: float = 30.0) -> bool:
    """A source spanning a sector boundary can peak in either neighbour.

    A plume centred on 90 degrees with a 30-degree half-width covers sectors 60
    and 90 equally, so demanding one exactly would be testing the noise that
    breaks the tie.
    """
    assert peak is not None
    return abs(((peak - bearing + 180) % 360) - 180) <= width


def _station(
    name: str,
    *,
    source_bearing: float | None = None,
    needs_wind: bool = False,
    n: int = 20_000,
    seed: int = 0,
) -> pl.DataFrame:
    """Synthetic hours with a known source geometry.

    ``source_bearing`` None means no directional source. ``needs_wind`` puts
    the plume at high wind speed (a distant source) rather than low (a near
    one).
    """
    rng = np.random.default_rng(seed)
    start = datetime(2010, 1, 1)

    bearing = rng.uniform(0, 360, n)
    speed = rng.gamma(2.0, 1.5, n).clip(0.05, 14.0)
    pm = rng.gamma(4.0, 4.0, n)

    if source_bearing is not None:
        # Within 30 degrees of the source bearing, wrapping correctly.
        delta = np.abs(((bearing - source_bearing + 180) % 360) - 180)
        aligned = delta < 30
        right_speed = speed > 6.0 if needs_wind else speed < 2.0
        pm = pm + (aligned & right_speed) * 60.0

    return pl.DataFrame(
        {
            "station_name": [name] * n,
            "ts_local": [start + timedelta(hours=i) for i in range(n)],
            "PM2.5": pm,
            "WS_HR": speed,
            "WD_HR": bearing,
        }
    )


class TestSourceGeometry:
    def test_a_nearby_source_peaks_at_low_wind(self) -> None:
        """Weak wind lets a local plume sit over the station."""
        result = cbpf(_station("近源", source_bearing=90.0), station="近源")

        assert result is not None
        assert _within_one_sector(result.peak_sector, 90.0)
        assert result.peak_speed in {"<0.5", "0.5-1.5", "1.5-2.5"}

    def test_a_distant_source_peaks_at_high_wind(self) -> None:
        """It takes wind to carry pollution a long way."""
        result = cbpf(
            _station("遠源", source_bearing=300.0, needs_wind=True, seed=1), station="遠源"
        )

        assert result is not None
        assert _within_one_sector(result.peak_sector, 300.0)
        assert result.peak_speed in {"6-8", "8+"}

    def test_the_two_are_distinguishable_on_speed_alone(self) -> None:
        """The point of the bivariate form.

        A one-dimensional wind rose would put both of these at their bearing
        and say nothing about how far away the source is.
        """
        near = cbpf(_station("近", source_bearing=90.0), station="近")
        far = cbpf(_station("遠", source_bearing=90.0, needs_wind=True, seed=2), station="遠")

        assert near is not None and far is not None
        assert near.peak_sector == far.peak_sector, "same bearing by construction"
        assert near.peak_speed != far.peak_speed, "different distance, different speed bin"


class TestDirectionalConcentration:
    def test_one_bearing_gives_a_high_resultant(self) -> None:
        grid = pl.DataFrame({"sector": [0, 90, 180, 270], "probability": [0.9, 0.02, 0.02, 0.02]})

        assert directional_concentration(grid) > 0.8

    def test_every_bearing_equally_gives_a_low_resultant(self) -> None:
        grid = pl.DataFrame({"sector": [0, 90, 180, 270], "probability": [0.3, 0.3, 0.3, 0.3]})

        assert directional_concentration(grid) < 0.05

    def test_a_directional_station_scores_above_a_uniform_one(self) -> None:
        directional = cbpf(_station("有向", source_bearing=210.0, seed=3), station="有向")
        uniform = cbpf(_station("無向", seed=4), station="無向")

        assert directional is not None and uniform is not None
        assert directional.resultant > uniform.resultant

    def test_an_empty_grid_is_undefined_rather_than_zero(self) -> None:
        empty = pl.DataFrame(schema={"sector": pl.Int32, "probability": pl.Float64})

        assert np.isnan(directional_concentration(empty))


class TestSuppression:
    def test_a_sparse_bin_is_withheld_not_reported_as_clean(self) -> None:
        """The failure this project exists to avoid, in polar form.

        A bearing the wind almost never comes from has few observations and
        possibly zero high hours. Reported as 0.0 it renders as reliably clean;
        reported as null it renders as unknown, which is the truth.
        """
        frame = _station("稀疏", seed=5, n=3000)
        # Make one sector nearly empty.
        frame = frame.filter(~pl.col("WD_HR").is_between(120, 150) | (pl.arange(0, 3000) % 50 == 0))

        result = cbpf(frame, station="稀疏", min_count=20)

        assert result is not None
        sparse = result.grid.filter(pl.col("sector").is_between(120, 149))
        withheld = sparse.filter(~pl.col("reliable"))
        assert withheld.height > 0
        assert withheld["probability"].is_null().all(), "withheld, not zero"

    def test_the_number_of_withheld_bins_is_reported(self) -> None:
        """Raising the per-bin threshold must not disqualify the station itself.

        `min_count` decides whether a bin is reportable; whether there is
        enough data to attempt CBPF at all is a separate question with its own
        constant. Sharing one number between them meant this call returned None.
        """
        result = cbpf(_station("站", seed=6), station="站", min_count=100_000)

        assert result is not None
        assert result.n_suppressed == result.grid.height, "everything withheld at this threshold"


class TestCalmHandling:
    def test_calm_hours_are_counted_and_excluded(self) -> None:
        """No wind means no bearing, but the hours still happened."""
        result = cbpf(_station("站", seed=7), station="站")

        assert result is not None
        assert result.n_calm > 0
        assert 0.0 < result.calm_fraction < 1.0
        # Every hour in the grid came from a moving-air hour.
        assert result.grid["n"].sum() == result.n_hours - result.n_calm


class TestWraparound:
    def test_bearing_360_lands_in_the_north_sector(self) -> None:
        """The mistake found once already, in the module next door."""
        n = 4000
        frame = pl.DataFrame(
            {
                "station_name": ["站"] * n,
                "ts_local": [datetime(2010, 1, 1) + timedelta(hours=i) for i in range(n)],
                "PM2.5": [20.0] * n,
                "WS_HR": [3.0] * n,
                "WD_HR": [360.0 if i % 2 else 0.0 for i in range(n)],
            }
        )

        result = cbpf(frame, station="站")

        assert result is not None
        assert result.grid["sector"].unique().to_list() == [0]


class TestThreshold:
    def test_the_threshold_is_the_requested_percentile(self) -> None:
        result = cbpf(_station("站", seed=8), station="站", percentile=90.0)
        lower = cbpf(_station("站", seed=8), station="站", percentile=50.0)

        assert result is not None
        assert lower is not None
        assert result.percentile == 90.0
        assert result.threshold > lower.threshold

    def test_too_few_hours_returns_nothing_rather_than_a_noisy_answer(self) -> None:
        assert cbpf(_station("站", n=50), station="站") is None

    def test_an_unknown_station_returns_nothing(self) -> None:
        assert cbpf(_station("站"), station="不存在") is None


class TestConstants:
    def test_the_calm_threshold_matches_the_anemometer_limit(self) -> None:
        """Most vanes cannot resolve direction below roughly 0.5 m/s."""
        assert pytest.approx(0.5) == sources.CALM_THRESHOLD_MS

    def test_speed_bins_are_ascending(self) -> None:
        bins = sources.DEFAULT_SPEED_BINS
        assert list(bins) == sorted(bins)
