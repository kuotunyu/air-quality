"""Parse raw measurement cells into a value and a quality flag.

MOENV encodes quality information inside the value cell itself, and the
convention changed between archive generations (see docs/archive-formats.md):

* **legacy** — the flag is a *suffix* on a retained measurement: ``15#``
* **modern** — the flag *replaces* the measurement entirely: ``#``

That difference matters scientifically: in legacy files a rejected reading can
still be inspected, in modern files it is gone. Both forms are parsed here so
downstream code never sees a raw token, and ``value_retained`` records which
convention produced each row.

Official legend, quoted from ``ReadMe_普通測站20170301.odt`` shipped inside the
archives::

    #     表示儀器檢核為無效值
    *     表示程式檢核為無效值
    x     表示人工檢核為無效值
    NR    表示無降雨
    空白  表示缺值

Two implementations live here and are kept honest by
``tests/test_flags.py::test_vectorised_matches_reference``:

* :func:`parse_token` — the scalar reference. This is the specification.
* :func:`parse_expr`  — a Polars expression used in production, because the
  full archive is ~10^8 cells and a Python-level call per cell is not viable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

import polars as pl

__all__ = [
    "Flag",
    "ParsedValue",
    "parse_expr",
    "parse_token",
]


class Flag(StrEnum):
    """Normalised quality state of a single measurement."""

    VALID = "valid"

    # Official invalidation categories.
    INSTRUMENT_CHECK_INVALID = "instrument_check_invalid"  # '#' 儀器檢核
    PROGRAM_CHECK_INVALID = "program_check_invalid"  # '*' 程式檢核
    MANUAL_CHECK_INVALID = "manual_check_invalid"  # 'x' 人工檢核

    # Semantic markers — these are information, not failures.
    NO_RAIN = "no_rain"  # 'NR' 無降雨
    RAIN_PRESENT = "rain_present"  # 'A'  (modern archives only)
    NOT_APPLICABLE = "not_applicable"  # 'NA' (modern archives only)
    BELOW_DETECTION_LIMIT = "below_detection_limit"  # 'ND'

    # Wind-direction sentinel codes, applied in twair.qc.sentinels.
    #
    # Which code means what is era-dependent and the two official ReadMe
    # editions disagree — see twair.qc.sentinels for the measurement that
    # settles it for the years these codes actually occur in.
    CALM = "calm"  # 靜風 — there was no wind, so direction is undefined
    VARIABLE_DIRECTION = "variable_direction"  # 風向不定 — wind too light/shifting to resolve
    INSTRUMENT_FAULT = "instrument_fault"  # 儀器故障

    OUT_OF_RANGE = "out_of_range"  # outside the physically plausible domain

    MISSING = "missing"  # empty cell 空白
    UNPARSEABLE = "unparseable"  # anything unrecognised — never silently dropped


# Raw token -> flag. Mirrors conf/qc.yaml; kept in code so the parser has no
# I/O dependency and can be unit tested in isolation.
FLAG_TOKENS: dict[str, Flag] = {
    "#": Flag.INSTRUMENT_CHECK_INVALID,
    "*": Flag.PROGRAM_CHECK_INVALID,
    "x": Flag.MANUAL_CHECK_INVALID,
    "X": Flag.MANUAL_CHECK_INVALID,
    "NR": Flag.NO_RAIN,
    "nr": Flag.NO_RAIN,
    "A": Flag.RAIN_PRESENT,
    "NA": Flag.NOT_APPLICABLE,
    "na": Flag.NOT_APPLICABLE,
    "N/A": Flag.NOT_APPLICABLE,
    "ND": Flag.BELOW_DETECTION_LIMIT,
    "nd": Flag.BELOW_DETECTION_LIMIT,
    "-": Flag.MISSING,
    "--": Flag.MISSING,
}

# Modern archives occasionally stack a semantic marker onto an invalidation,
# e.g. `NA#` (observed 87 times in 2023 alone). The legend does not describe
# these, but the intent is unambiguous: the reading was rejected. Generating
# the combinations keeps both parser implementations table-driven rather than
# growing bespoke decomposition logic.
_SEMANTIC = ("NR", "nr", "NA", "na", "ND", "nd", "A")
_INVALIDATION = ("#", "*", "x", "X")

for _semantic in _SEMANTIC:
    for _invalid in _INVALIDATION:
        # Rejection outranks the semantic note — a value marked "not applicable
        # and instrument-failed" is, above all, unusable.
        FLAG_TOKENS.setdefault(_semantic + _invalid, FLAG_TOKENS[_invalid])
        FLAG_TOKENS.setdefault(_invalid + _semantic, FLAG_TOKENS[_invalid])

del _semantic, _invalid

# Splits a cell into an optional numeric prefix and an optional trailing marker.
# The numeric alternation tolerates MOENV's leading-dot decimals (`.33`), which
# appear throughout the Big5-era CSVs.
_CELL = re.compile(r"^\s*(-?(?:\d+\.?\d*|\.\d+))?\s*(.*?)\s*$")

# Same grammar, as two Polars-compatible capture patterns.
_NUM_PATTERN = r"^\s*(-?(?:\d+\.?\d*|\.\d+))"
_REST_PATTERN = r"^\s*(?:-?(?:\d+\.?\d*|\.\d+))?\s*(.*?)\s*$"


@dataclass(frozen=True, slots=True)
class ParsedValue:
    """One measurement cell, decomposed."""

    value: float | None
    flag: Flag
    raw: str
    value_retained: bool
    """True when an invalidation flag arrived alongside a usable number.

    Only legacy archives can produce this. It is the marker that lets Phase 2
    quantify how much information the modern format silently discards.
    """


def parse_token(raw: str | None) -> ParsedValue:
    """Decompose one raw cell. This function is the specification.

    >>> parse_token("12.3")
    ParsedValue(value=12.3, flag=<Flag.VALID: 'valid'>, raw='12.3', value_retained=False)
    >>> parse_token("15#").flag
    <Flag.INSTRUMENT_CHECK_INVALID: 'instrument_check_invalid'>
    >>> parse_token("15#").value
    15.0
    >>> parse_token("#").value is None
    True
    """
    if raw is None:
        return ParsedValue(None, Flag.MISSING, "", value_retained=False)

    text = raw.strip()
    if not text:
        return ParsedValue(None, Flag.MISSING, raw, value_retained=False)

    match = _CELL.match(text)
    number_part = match.group(1) if match else None
    marker = (match.group(2) if match else text) or ""

    if number_part is None:
        flag = FLAG_TOKENS.get(marker, Flag.UNPARSEABLE)
        return ParsedValue(None, flag, raw, value_retained=False)

    value = float(number_part)

    if not marker:
        return ParsedValue(value, Flag.VALID, raw, value_retained=False)

    flag = FLAG_TOKENS.get(marker)
    if flag is None:
        # A number followed by something we do not recognise: keep the raw cell
        # visible rather than guessing.
        return ParsedValue(None, Flag.UNPARSEABLE, raw, value_retained=False)

    # Legacy suffix form: the reading survived its own rejection.
    return ParsedValue(value, flag, raw, value_retained=True)


def _flag_mapping_expr(marker: pl.Expr) -> pl.Expr:
    """Map a marker string to a Flag value, defaulting to UNPARSEABLE."""
    expr = pl.when(marker == "").then(pl.lit(Flag.MISSING.value))
    for token, flag in FLAG_TOKENS.items():
        expr = expr.when(marker == token).then(pl.lit(flag.value))
    return expr.otherwise(pl.lit(Flag.UNPARSEABLE.value))


def parse_expr(column: str | pl.Expr) -> pl.Expr:
    """Vectorised equivalent of :func:`parse_token`, returning a struct column.

    The struct fields are ``value``, ``flag`` and ``value_retained``. Applied to
    the whole archive this is roughly three orders of magnitude faster than
    mapping :func:`parse_token` over each cell.
    """
    col = pl.col(column) if isinstance(column, str) else column
    text = col.cast(pl.Utf8).str.strip_chars()

    number = text.str.extract(_NUM_PATTERN, 1)
    marker = text.str.extract(_REST_PATTERN, 1).fill_null("")

    has_number = number.is_not_null()
    has_marker = marker != ""
    flag_from_marker = _flag_mapping_expr(marker)

    # A number with an unrecognised trailing marker is not trustworthy.
    unparseable_suffix = has_number & has_marker & (flag_from_marker == Flag.UNPARSEABLE.value)

    value = (
        pl.when(has_number & ~unparseable_suffix)
        .then(number.cast(pl.Float64, strict=False))
        .otherwise(None)
    )

    flag = (
        pl.when(~has_number)
        .then(flag_from_marker)
        .when(~has_marker)
        .then(pl.lit(Flag.VALID.value))
        .otherwise(flag_from_marker)
    )

    value_retained = has_number & has_marker & ~unparseable_suffix

    return pl.struct(
        value.alias("value"),
        flag.alias("flag"),
        value_retained.alias("value_retained"),
    )
