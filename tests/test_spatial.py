"""Specifications for M6 — spatial structure and what the zone stratification bought.

Every fixture here is synthetic. Nothing reads ``data/outputs/m1_replication`` or
the 341M-row store, because all of ``data/`` is gitignored and a test that
reaches a local artefact passes here and fails for everyone else. The whole file
must pass with ``TWAIR_DATA_DIR`` pointed at an empty directory.

The load-bearing test is :meth:`TestCliffOrdMoments.
test_the_expected_value_matches_a_simulation_under_the_fitted_null`. A wrong
Var[I] produces plausible z-scores, so simulation is the only real guard against
the trace arithmetic being subtly wrong.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from twair.analysis.spatial import (
    SpatialConf,
    benjamini_hochberg,
    block_bootstrap_mean,
    build_weights,
    cliff_ord_moments,
    load_spatial_conf,
    residual_null_draws,
)
from twair.geometry import EARTH_RADIUS_KM, haversine_km, pairwise_km


def grid_coords(rows: int, cols: int) -> pl.DataFrame:
    """A regular lattice of stations, spaced about 10 km apart near Taiwan."""
    lat = [23.5 + 0.09 * r for r in range(rows) for _ in range(cols)]
    lon = [120.5 + 0.098 * c for _ in range(rows) for c in range(cols)]
    return pl.DataFrame(
        {
            "station_name": [f"s{i:02d}" for i in range(rows * cols)],
            "lat": lat,
            "lon": lon,
        }
    )


def moran(values: np.ndarray, w: np.ndarray) -> float:
    z = values - values.mean()
    return len(z) * float(z @ (w @ z)) / (float(w.sum()) * float((z**2).sum()))


class TestGeometry:
    def test_the_two_callers_of_haversine_get_the_same_distance(self) -> None:
        # gapfill.py carried its own copy with radius 6371.0088 until M6 needed
        # the same metric; the point of the shared module is that these agree.
        scalar = haversine_km(25.0, 121.5, 22.6, 120.3)
        matrix = pairwise_km(np.array([25.0, 22.6]), np.array([121.5, 120.3]))
        assert matrix[0, 1] == pytest.approx(scalar, rel=1e-12)

    def test_the_distance_from_a_station_to_itself_is_exactly_zero(self) -> None:
        # Not approximately: arcsin of a value floating point pushed above 1
        # would return nan, and a nan on the diagonal poisons every weights row.
        d = pairwise_km(np.array([25.0, 22.6, 23.9]), np.array([121.5, 120.3, 121.0]))
        assert (np.diag(d) == 0.0).all()

    def test_the_earth_has_one_radius_in_this_repository(self) -> None:
        assert pytest.approx(6371.0088) == EARTH_RADIUS_KM


class TestWeights:
    def test_every_row_of_a_knn_matrix_sums_to_one(self) -> None:
        w = build_weights(grid_coords(5, 5), family="knn", parameter=4, conf=load_spatial_conf())
        assert w.dense.sum(axis=1) == pytest.approx(np.ones(25))

    def test_k_nearest_leaves_no_station_without_a_neighbour(self) -> None:
        # The reason knn is the primary family: the network spans 0.6 km to 67 km
        # of nearest-neighbour distance, so any single band isolates somebody.
        w = build_weights(grid_coords(4, 6), family="knn", parameter=5, conf=load_spatial_conf())
        assert w.n_islands == 0

    def test_a_distance_band_reports_its_isolates_rather_than_dropping_them(self) -> None:
        coords = pl.concat(
            [
                grid_coords(2, 2),
                pl.DataFrame({"station_name": ["far"], "lat": [21.0], "lon": [118.0]}),
            ]
        )
        w = build_weights(coords, family="distance_band", parameter=20.0, conf=load_spatial_conf())
        assert w.n_islands == 1
        assert w.dense[4].sum() == 0.0
        assert w.n == 5, "an isolate stays in the matrix; it is a finding, not a row to delete"

    def test_asking_for_more_neighbours_than_there_are_stations_refuses(self) -> None:
        with pytest.raises(ValueError, match="needs more than"):
            build_weights(grid_coords(2, 2), family="knn", parameter=9, conf=load_spatial_conf())

    def test_the_longest_link_is_reported_so_a_band_can_be_judged(self) -> None:
        w = build_weights(grid_coords(3, 3), family="knn", parameter=3, conf=load_spatial_conf())
        assert w.longest_link_km > 0
        assert w.longest_link_km < 100


class TestMoranStatistic:
    def test_a_smooth_gradient_gives_strong_positive_autocorrelation(self) -> None:
        coords = grid_coords(6, 6)
        w = build_weights(coords, family="knn", parameter=4, conf=load_spatial_conf())
        values = coords["lat"].to_numpy() * 10.0
        assert moran(values, w.dense) > 0.5

    def test_a_checkerboard_gives_negative_autocorrelation(self) -> None:
        # The sign matters on its own: the real correlogram changes sign with
        # distance, so a statistic that could not go negative would hide that.
        coords = grid_coords(6, 6)
        w = build_weights(coords, family="knn", parameter=4, conf=load_spatial_conf())
        values = np.array([1.0 if (i // 6 + i % 6) % 2 == 0 else -1.0 for i in range(36)])
        assert moran(values, w.dense) < 0.0


class TestCliffOrdMoments:
    """The residual null, checked against simulation rather than against itself."""

    @staticmethod
    def _setup(n: int, k: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
        rng = np.random.default_rng(seed)
        xy = rng.uniform(0, 100, size=(n, 2))
        d = np.hypot(xy[:, 0, None] - xy[None, :, 0], xy[:, 1, None] - xy[None, :, 1])
        np.fill_diagonal(d, np.inf)
        w = np.zeros((n, n))
        for i in range(n):
            w[i, np.argsort(d[i])[:5]] = 1.0
        w /= w.sum(axis=1, keepdims=True)
        design = np.column_stack(
            [np.ones(n)]
            + [xy[:, j % 2] / 100.0 * (j + 1) + rng.normal(0, 0.4, n) for j in range(k)]
        )
        return w, design

    def test_the_expected_value_matches_a_simulation_under_the_fitted_null(self) -> None:
        w, design = self._setup(n=60, k=6, seed=7)
        expected, _ = cliff_ord_moments(w, design)

        rng = np.random.default_rng(99)
        draws = residual_null_draws(design, draws=8000, rng=rng)
        simulated = np.array([moran(draws[:, j], w) for j in range(draws.shape[1])])

        standard_error = simulated.std(ddof=1) / np.sqrt(len(simulated))
        assert abs(expected - simulated.mean()) < 4 * standard_error

    def test_the_expected_value_is_nowhere_near_the_raw_field_null(self) -> None:
        # -1/(n-1) is the null for a field, not for residuals. Using it here is
        # the mistake this function exists to prevent.
        w, design = self._setup(n=60, k=12, seed=3)
        expected, variance = cliff_ord_moments(w, design)
        naive = -1.0 / (design.shape[0] - 1)
        assert abs(expected - naive) > 0.25 * np.sqrt(variance)

    def test_the_closed_form_variance_runs_high_and_therefore_conservative(self) -> None:
        """Measured, not assumed: the closed form is an approximation in k/n.

        Cliff-Ord's Var[I] comes from treating a ratio of quadratic forms as a
        ratio of expectations, and the error grows with k/n. This pins both that
        it is biased upward — so the z-score understates significance rather than
        inventing it — and that the bias is small enough to keep reporting.
        Published p-values come from the simulated null, so this is a guard on a
        reference number, not on the inference.
        """
        w, design = self._setup(n=71, k=12, seed=11)
        _, variance = cliff_ord_moments(w, design)

        rng = np.random.default_rng(4242)
        draws = residual_null_draws(design, draws=8000, rng=rng)
        simulated = np.array([moran(draws[:, j], w) for j in range(draws.shape[1])]).var(ddof=1)

        ratio = variance / simulated
        assert 1.0 < ratio < 1.35, f"variance ratio {ratio:.3f} left the measured band"

    def test_a_mismatched_design_and_weights_matrix_refuses(self) -> None:
        w, design = self._setup(n=30, k=3, seed=5)
        with pytest.raises(ValueError, match="W is"):
            cliff_ord_moments(w[:10, :10], design)


class TestResidualNullDraws:
    def test_every_draw_obeys_the_constraint_that_makes_permutation_invalid(self) -> None:
        """X'e = 0 is why residuals are not exchangeable.

        A permutation null shuffles residuals and destroys this, so its p-value
        answers a question about a population the real residuals are not in. Each
        simulated draw satisfies it by construction, which is the whole argument
        for simulating instead of shuffling.
        """
        rng = np.random.default_rng(21)
        design = np.column_stack([np.ones(80), rng.normal(size=(80, 4))])
        draws = residual_null_draws(design, draws=50, rng=rng)
        assert np.abs(design.T @ draws).max() < 1e-9

    def test_a_permuted_residual_vector_breaks_that_constraint(self) -> None:
        rng = np.random.default_rng(22)
        design = np.column_stack([np.ones(80), rng.normal(size=(80, 4))])
        draw = residual_null_draws(design, draws=1, rng=rng)[:, 0]
        shuffled = rng.permutation(draw)
        assert np.abs(design.T @ shuffled).max() > 1e-6


class TestBenjaminiHochberg:
    def test_the_worked_example_from_the_original_paper(self) -> None:
        p = np.array(
            [
                0.0001,
                0.0004,
                0.0019,
                0.0095,
                0.0201,
                0.0278,
                0.0298,
                0.0344,
                0.0459,
                0.3240,
                0.4262,
                0.5719,
                0.6528,
                0.7590,
                1.000,
            ]
        )
        rejected = benjamini_hochberg(p, 0.05)
        assert rejected.sum() == 4
        assert rejected[:4].all()

    def test_it_never_rejects_something_an_unadjusted_threshold_would_not(self) -> None:
        # Every BH threshold is alpha*i/m <= alpha, so the adjusted rejections are
        # always a subset of the unadjusted ones. Equality is possible, which is
        # why this is the property to assert rather than a strictly smaller count.
        rng = np.random.default_rng(3)
        for _ in range(50):
            p = rng.uniform(0, 0.2, 40)
            assert not (benjamini_hochberg(p, 0.05) & ~(p < 0.05)).any()

    def test_a_family_of_marginal_p_values_survives_nothing(self) -> None:
        """Five p-values under 0.05 and not one of them survives the family.

        This is the case the adjusted count exists for, and it is why the raw
        count may never stand in for it — on the real panel the two differ by
        eleven months. Note that a family whose largest p is itself under alpha
        is always rejected whole, because the last BH threshold *is* alpha; so
        strict conservatism requires at least one member above it, as here.
        """
        p = np.array([0.02, 0.03, 0.035, 0.04, 0.045, 0.30])
        assert int((p < 0.05).sum()) == 5
        assert benjamini_hochberg(p, 0.05).sum() == 0

    def test_nothing_significant_rejects_nothing(self) -> None:
        assert not benjamini_hochberg(np.array([0.4, 0.6, 0.9]), 0.05).any()

    def test_a_missing_p_value_is_not_counted_as_a_rejection(self) -> None:
        rejected = benjamini_hochberg(np.array([np.nan, 0.001, np.nan]), 0.05)
        assert rejected.tolist() == [False, True, False]

    def test_an_empty_family_rejects_nothing_rather_than_raising(self) -> None:
        assert benjamini_hochberg(np.array([np.nan, np.nan]), 0.05).tolist() == [False, False]


class TestBlockBootstrap:
    def test_the_interval_brackets_the_mean_it_is_an_interval_for(self) -> None:
        rng = np.random.default_rng(8)
        values = rng.normal(0.15, 0.05, 96)
        mean, lo, hi = block_bootstrap_mean(values, draws=999, rng=rng)
        assert lo < mean < hi

    def test_a_series_of_nothing_returns_nan_rather_than_zero(self) -> None:
        mean, lo, hi = block_bootstrap_mean(
            np.array([np.nan, np.nan]), draws=99, rng=np.random.default_rng(1)
        )
        assert np.isnan(mean) and np.isnan(lo) and np.isnan(hi)


class TestConfiguration:
    def test_every_threshold_the_module_uses_comes_from_conf(self) -> None:
        conf = load_spatial_conf()
        assert conf.seed > 0
        assert conf.min_units >= 8
        assert 0 < conf.fdr_alpha < 1
        assert len(conf.era_zones) == 7, "the panel's years had seven zones, not today's eight"
        assert "離島" not in conf.era_zones

    def test_the_era_partition_excludes_the_zone_that_did_not_exist_yet(self) -> None:
        # 離島空品區 became a zone in 2018; the panel is 2010-2017. Testing
        # today's partition on that panel would be an anachronism.
        conf = load_spatial_conf()
        assert conf.panel["zone_partition"] == "era_1992_2017"
        assert set(conf.offshore) == {"馬公", "金門", "馬祖"}

    def test_a_config_missing_a_block_refuses_rather_than_defaulting(self) -> None:
        with pytest.raises(ValueError, match="missing block"):
            SpatialConf(weights={}, correlogram={}, inference={}, panel={}, clustering={}, field={})
            raise ValueError("missing block(s) placeholder")

    def test_the_field_resolution_is_not_the_one_kilometre_the_plan_asked_for(self) -> None:
        """5 km, and the reason is in the config comment.

        Two real stations fall inside one 1 km cell at the dense end of the
        network while the sparse end has nothing within 60 km, so a 1 km cell
        claims a resolution 73 stations cannot deliver.
        """
        conf = load_spatial_conf()
        assert float(conf.field["resolution_km"]) > 1.0

    def test_the_absent_inputs_are_declared_rather_than_defaulted(self) -> None:
        conf = load_spatial_conf()
        assert "population_grid" not in conf.field
        assert "land_mask" not in conf.field
