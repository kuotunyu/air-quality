"""Reading a single number out of Polars without lying to the type checker.

``Series.sum()``, ``.mean()``, ``.min()`` and ``.max()`` are typed as a union of
every value a cell could hold — ``int | float | Decimal | date | time |
timedelta | str | bytes | ndarray | ...`` — because in principle they can be
called on any column. Mypy cannot narrow that, so every aggregate read in
strict mode produces an error, and the obvious local fix is a ``cast`` at each
site. Six of those accumulated in one pass before this module existed.

Scattering them has two costs. The reason for each is invisible, so a reader
cannot tell a Polars typing limitation from someone silencing a real defect.
And a ``cast(int, ...)`` is a claim to the checker with no runtime force
whatsoever — if the value is actually None because the column was empty, the
cast says nothing and the failure surfaces somewhere else entirely.

These do the conversion for real, and refuse an empty aggregate rather than
returning a plausible zero. `Series.sum()` over no rows is 0 in Polars, but
`.mean()` and `.min()` are None, and a null that becomes 0.0 on the way into a
report is exactly the class of bug this project exists to document.
"""

from __future__ import annotations

from typing import Any

__all__ = ["as_float", "as_int", "opt_float"]


def as_float(value: Any, *, what: str = "value") -> float:
    """Coerce a Polars aggregate to float, refusing null.

    Raises rather than substituting a default: an aggregate over an empty
    selection has no answer, and the caller is better placed to decide what
    that means than this function is.
    """
    if value is None:
        raise ValueError(f"{what} is null — the aggregate had nothing to work on")
    return float(value)


def as_int(value: Any, *, what: str = "value") -> int:
    """Coerce a Polars aggregate to int, refusing null."""
    if value is None:
        raise ValueError(f"{what} is null — the aggregate had nothing to work on")
    return int(value)


def opt_float(value: Any) -> float | None:
    """Coerce to float, passing null through.

    For the cases where an absent aggregate is a legitimate answer and the
    caller wants to keep it absent rather than raise.
    """
    return None if value is None else float(value)
