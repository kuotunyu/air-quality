"""Tests for the long table's primary key.

`twair.store.schema`'s own first paragraph says the long form is one row per
(station, timestamp, pollutant). `validate` checked the dtypes, the nulls, the
flag vocabulary and the valid-with-no-value case, and did not check that. The
invariant was violated from the first build onwards without anything failing:
the 1999 archive files ten 雲嘉南 stations under 北部空品區 as well, the parser
read both members, and 1,071,168 station-hours were stored twice.

The means survived it — duplicating every row of a station-year leaves its
average alone — which is why nothing looked wrong. The counts did not: 33,104
station-days came out with a coverage ratio above 1.0, up to exactly 2.0, and
578 daily means were published that the 16-of-24 rule should have withheld.
"""

from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from twair.build import collapse_duplicate_members
from twair.store.schema import KEY_COLUMNS, SchemaError, conform, validate


def _rows(*, members: list[str], value: float | None = 12.5, flag: str = "valid") -> pl.DataFrame:
    """One station-hour, once per source member — the shape the 1999 bug made."""
    n = len(members)
    return conform(
        pl.DataFrame(
            {
                "station_name": ["新營"] * n,
                "pollutant": ["PM10"] * n,
                "ts_local": [datetime(1999, 7, 30, 17)] * n,
                "value": [value] * n,
                "flag": [flag] * n,
                "value_retained": [False] * n,
                "imputed": [False] * n,
                "impute_method": [None] * n,
                "generation": ["legacy_csv_big5"] * n,
                "source_member": members,
                "year": [1999] * n,
                "month": [7] * n,
            }
        )
    )


ZONES = [
    "88年北部空品區/88年新營站_20081006.csv",
    "88年雲嘉南空品區/88年新營站_20081006.csv",
]


class TestValidateRejectsDuplicateKeys:
    def test_the_same_station_hour_twice_is_a_schema_error(self) -> None:
        with pytest.raises(SchemaError, match="duplicate"):
            validate(_rows(members=ZONES))

    def test_the_error_names_the_key_so_the_next_reader_knows_what_one_row_means(self) -> None:
        with pytest.raises(SchemaError) as caught:
            validate(_rows(members=ZONES))

        for column in KEY_COLUMNS:
            assert column in str(caught.value)

    def test_one_row_per_station_hour_passes(self) -> None:
        assert validate(_rows(members=ZONES[:1])).height == 1

    def test_two_pollutants_at_the_same_hour_are_not_duplicates(self) -> None:
        frame = pl.concat(
            [
                _rows(members=ZONES[:1]),
                _rows(members=ZONES[:1]).with_columns(
                    pl.lit("SO2").cast(pl.Categorical).alias("pollutant")
                ),
            ]
        )

        assert validate(frame).height == 2


class TestCollapseDuplicateMembers:
    def test_two_identical_copies_become_one_and_the_drop_is_counted(self) -> None:
        """Silently correct output with an uncounted repair is what got us here."""
        collapsed, dropped = collapse_duplicate_members(_rows(members=ZONES))

        assert collapsed.height == 1
        assert dropped == 1

    def test_a_frame_with_no_duplicates_is_returned_untouched(self) -> None:
        frame = _rows(members=ZONES[:1])
        collapsed, dropped = collapse_duplicate_members(frame)

        assert dropped == 0
        assert collapsed.equals(frame)

    def test_the_survivor_is_the_same_whichever_order_the_members_arrived(self) -> None:
        """Two identical rows make the choice arbitrary; it must not be random."""
        forward, _ = collapse_duplicate_members(_rows(members=ZONES))
        backward, _ = collapse_duplicate_members(_rows(members=list(reversed(ZONES))))

        assert forward["source_member"].to_list() == backward["source_member"].to_list()

    def test_the_result_passes_the_validation_that_rejected_the_input(self) -> None:
        collapsed, _ = collapse_duplicate_members(_rows(members=ZONES))

        assert validate(collapsed).height == 1

    def test_copies_that_disagree_on_the_reading_are_refused_not_collapsed(self) -> None:
        """Two copies of one hour with different numbers is not a packaging artefact."""
        frame = pl.concat([_rows(members=ZONES[:1]), _rows(members=ZONES[1:], value=99.0)])

        with pytest.raises(ValueError, match="different readings"):
            collapse_duplicate_members(frame)

    def test_copies_that_disagree_on_the_flag_are_refused(self) -> None:
        frame = pl.concat(
            [_rows(members=ZONES[:1]), _rows(members=ZONES[1:], flag="instrument_check_invalid")]
        )

        with pytest.raises(ValueError, match="different readings"):
            collapse_duplicate_members(frame)

    def test_a_copy_with_a_number_and_a_copy_without_one_disagree(self) -> None:
        """A null and a reading are the distinction this project exists to keep."""
        frame = pl.concat(
            [
                _rows(members=ZONES[:1], value=None, flag="missing"),
                _rows(members=ZONES[1:], value=12.5, flag="missing"),
            ]
        )

        with pytest.raises(ValueError, match="different readings"):
            collapse_duplicate_members(frame)

    def test_two_copies_that_are_both_missing_collapse(self) -> None:
        """Null equals null here: the hour was recorded twice as unmeasured."""
        collapsed, dropped = collapse_duplicate_members(
            _rows(members=ZONES, value=None, flag="missing")
        )

        assert collapsed.height == 1
        assert dropped == 1
