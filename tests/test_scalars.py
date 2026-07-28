"""Tests for reading single numbers out of Polars.

The point of these helpers is not the conversion — `float()` already does that.
It is that an aggregate over nothing raises instead of returning a plausible
number, which is the same rule the rest of the pipeline follows.
"""

from __future__ import annotations

import polars as pl
import pytest

from twair.scalars import as_float, as_int, opt_float


class TestRefusingNull:
    def test_a_null_aggregate_raises_rather_than_returning_zero(self) -> None:
        """`Series.mean()` over an empty selection is None, not 0.

        Letting that become 0.0 puts a confident number in a report that had no
        data behind it — the failure this project exists to document.
        """
        empty = pl.Series("x", [], dtype=pl.Float64)

        with pytest.raises(ValueError, match="null"):
            as_float(empty.mean())

    def test_the_error_names_what_was_being_read(self) -> None:
        with pytest.raises(ValueError, match="first year"):
            as_int(None, what="first year")

    def test_an_empty_sum_is_zero_and_that_is_polars_not_us(self) -> None:
        """Documenting the asymmetry rather than papering over it.

        Polars returns 0 for a sum over no rows but None for a mean, so a sum
        passes this guard and a mean does not. That is worth knowing when
        reading a report built from both.
        """
        empty = pl.Series("x", [], dtype=pl.Int64)

        assert as_int(empty.sum()) == 0
        with pytest.raises(ValueError):
            as_float(empty.mean())


class TestConversion:
    def test_a_real_aggregate_converts(self) -> None:
        series = pl.Series("x", [1, 2, 3, 4])

        assert as_int(series.sum()) == 10
        assert as_float(series.mean()) == pytest.approx(2.5)

    def test_an_integer_aggregate_can_be_read_as_float(self) -> None:
        series = pl.Series("x", [1, 2, 3], dtype=pl.Int32)

        assert as_float(series.sum()) == pytest.approx(6.0)

    def test_opt_float_passes_null_through_for_callers_that_want_it(self) -> None:
        assert opt_float(None) is None
        assert opt_float(2) == pytest.approx(2.0)
