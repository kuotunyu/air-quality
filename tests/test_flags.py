"""Tests for measurement-cell parsing.

The corpus below includes every non-numeric token actually observed while
sampling the 1994, 2010 and 2024 archives, plus the numeric shapes MOENV uses.
"""

from __future__ import annotations

import polars as pl
import pytest

from twair.qc.flags import Flag, parse_expr, parse_token

# Tokens seen in real files, by generation.
LEGACY_SUFFIX_TOKENS = ["15#", "0.1#", "12.3*", "4.5x", "-0.2#", ".33#"]
MODERN_BARE_TOKENS = ["#", "*", "x", "NA", "A"]
SEMANTIC_TOKENS = ["NR", "ND"]
NUMERIC_TOKENS = ["0", "15", "12.3", ".33", "-0.5", "2.30", "0.0"]
EMPTY_TOKENS = ["", "   ", "-"]
ODDITIES = ["???", "abc", "12.3?", "1.2.3", "＃"]

CORPUS = (
    LEGACY_SUFFIX_TOKENS
    + MODERN_BARE_TOKENS
    + SEMANTIC_TOKENS
    + NUMERIC_TOKENS
    + EMPTY_TOKENS
    + ODDITIES
)


class TestNumbers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("0", 0.0), ("15", 15.0), ("12.3", 12.3), ("-0.5", -0.5), ("2.30", 2.3)],
    )
    def test_plain_numbers_are_valid(self, raw: str, expected: float) -> None:
        parsed = parse_token(raw)

        assert parsed.value == pytest.approx(expected)
        assert parsed.flag is Flag.VALID
        assert parsed.value_retained is False

    def test_leading_dot_decimal(self) -> None:
        """Big5-era CSVs write 0.33 as `.33`."""
        assert parse_token(".33").value == pytest.approx(0.33)

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert parse_token("  12.3  ").value == pytest.approx(12.3)


class TestLegacySuffixForm:
    """Pre-2018 archives keep the reading and append the flag."""

    @pytest.mark.parametrize(
        ("raw", "value", "flag"),
        [
            ("15#", 15.0, Flag.INSTRUMENT_CHECK_INVALID),
            ("0.1#", 0.1, Flag.INSTRUMENT_CHECK_INVALID),
            ("12.3*", 12.3, Flag.PROGRAM_CHECK_INVALID),
            ("4.5x", 4.5, Flag.MANUAL_CHECK_INVALID),
            ("-0.2#", -0.2, Flag.INSTRUMENT_CHECK_INVALID),
        ],
    )
    def test_value_survives_its_own_rejection(self, raw: str, value: float, flag: Flag) -> None:
        parsed = parse_token(raw)

        assert parsed.value == pytest.approx(value)
        assert parsed.flag is flag
        assert parsed.value_retained is True


class TestModernReplacementForm:
    """2018+ archives discard the reading entirely."""

    @pytest.mark.parametrize(
        ("raw", "flag"),
        [
            ("#", Flag.INSTRUMENT_CHECK_INVALID),
            ("*", Flag.PROGRAM_CHECK_INVALID),
            ("x", Flag.MANUAL_CHECK_INVALID),
            ("NA", Flag.NOT_APPLICABLE),
            ("A", Flag.RAIN_PRESENT),
        ],
    )
    def test_bare_marker_yields_no_value(self, raw: str, flag: Flag) -> None:
        parsed = parse_token(raw)

        assert parsed.value is None
        assert parsed.flag is flag
        assert parsed.value_retained is False

    def test_the_two_generations_are_distinguishable(self) -> None:
        """This distinction is the point: only one generation retains data."""
        legacy = parse_token("15#")
        modern = parse_token("#")

        assert legacy.flag is modern.flag
        assert legacy.value_retained != modern.value_retained
        assert legacy.value is not None and modern.value is None


class TestSemanticMarkers:
    def test_no_rain_is_information_not_absence(self) -> None:
        """`NR` means it genuinely did not rain — distinct from a missing reading.

        Both carry a null value, so the flag is the only thing separating "we
        measured, and it was zero" from "we do not know". The two tokens are
        parsed side by side here because asserting the distinction against a
        literal proves nothing: once ``flag is Flag.NO_RAIN`` has been checked,
        ``flag is not Flag.MISSING`` is true by construction.
        """
        no_rain = parse_token("NR")
        missing = parse_token("")

        assert no_rain.flag is Flag.NO_RAIN
        assert missing.flag is Flag.MISSING
        assert no_rain.value is None and missing.value is None

    def test_below_detection_limit_is_not_zero(self) -> None:
        parsed = parse_token("ND")

        assert parsed.flag is Flag.BELOW_DETECTION_LIMIT
        assert parsed.value is None, "must not be coerced to 0 — that biases means downward"


class TestMissingAndUnparseable:
    @pytest.mark.parametrize("raw", ["", "   ", "-", None])
    def test_empty_forms_are_missing(self, raw: str | None) -> None:
        parsed = parse_token(raw)

        assert parsed.value is None
        assert parsed.flag is Flag.MISSING

    @pytest.mark.parametrize("raw", ["???", "abc", "＃", "1.2.3"])
    def test_unrecognised_tokens_are_surfaced_not_dropped(self, raw: str) -> None:
        parsed = parse_token(raw)

        assert parsed.flag is Flag.UNPARSEABLE
        assert parsed.raw == raw, "the original cell must remain inspectable"

    def test_number_with_unknown_suffix_is_not_trusted(self) -> None:
        parsed = parse_token("12.3?")

        assert parsed.flag is Flag.UNPARSEABLE
        assert parsed.value is None


def test_vectorised_matches_reference() -> None:
    """The Polars implementation must agree with the scalar specification.

    Production parses ~10^8 cells with `parse_expr`; `parse_token` is the
    readable definition. They are only allowed to differ by being fast.
    """
    frame = pl.DataFrame({"raw": CORPUS})

    vectorised = frame.select(parse_expr("raw").alias("p")).unnest("p")
    reference = [parse_token(token) for token in CORPUS]

    for i, (token, expected) in enumerate(zip(CORPUS, reference, strict=True)):
        row = vectorised.row(i, named=True)
        assert row["flag"] == expected.flag.value, f"flag mismatch for {token!r}"
        assert row["value_retained"] == expected.value_retained, f"retained mismatch: {token!r}"
        if expected.value is None:
            assert row["value"] is None, f"value mismatch for {token!r}"
        else:
            assert row["value"] == pytest.approx(expected.value), f"value mismatch for {token!r}"


def test_vectorised_handles_nulls() -> None:
    frame = pl.DataFrame({"raw": [None, "12.3", ""]}, schema={"raw": pl.Utf8})

    out = frame.select(parse_expr("raw").alias("p")).unnest("p")

    assert out["flag"].to_list() == [Flag.MISSING.value, Flag.VALID.value, Flag.MISSING.value]
    assert out["value"].to_list() == [None, 12.3, None]


class TestCompoundMarkers:
    """Modern archives stack markers, e.g. `NA#` — undocumented but unambiguous."""

    @pytest.mark.parametrize("raw", ["NA#", "NA*", "NAx", "NR#", "ND*", "A#"])
    def test_rejection_outranks_the_semantic_note(self, raw: str) -> None:
        parsed = parse_token(raw)

        assert parsed.flag in {
            Flag.INSTRUMENT_CHECK_INVALID,
            Flag.PROGRAM_CHECK_INVALID,
            Flag.MANUAL_CHECK_INVALID,
        }
        assert parsed.value is None

    def test_compound_markers_agree_between_implementations(self) -> None:
        tokens = ["NA#", "NR*", "ND x".replace(" ", ""), "#NA", "12.5NA#"]
        frame = pl.DataFrame({"raw": tokens})

        vectorised = frame.select(parse_expr("raw").alias("p")).unnest("p")

        for i, token in enumerate(tokens):
            assert vectorised.row(i, named=True)["flag"] == parse_token(token).flag.value
