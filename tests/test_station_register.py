"""The one external table this project depends on.

Every other number here is derived from the archives. The archives carry no
coordinates, so the map on the entry page, the county rollups and chapter 2 all
rest on MOENV's station register — and `ingest/station_meta.py` was 56
statements at 0% coverage.

The module's own docstring names the failure it is guarding against: a station
without coordinates must be "counted in the open, never given a
plausible-looking guess", and a coordinate outside Taiwan must fail loudly
"rather than plotting it", because a null island or a swapped pair renders as a
station in the ocean and on a map that reads as a real place.

Those guards had never been executed. `validate_map_geometry.py` checks the
committed cache in CI, which is the downstream half; this is the upstream half,
where a bad register is supposed to be refused before it can be cached.
"""

from __future__ import annotations

import httpx
import polars as pl
import pytest
import respx

from twair.ingest.station_meta import (
    API_URL,
    TAIWAN_BOUNDS,
    _to_frame,
    fetch_station_register,
    reconcile_with_store,
)


def record(**over: object) -> dict[str, object]:
    """One register row, with every field the module declares it needs."""
    row: dict[str, object] = {
        "sitename": "板橋",
        "siteengname": "Banqiao",
        "areaname": "北部空品區",
        "county": "新北市",
        "township": "板橋區",
        "siteaddress": "新北市板橋區僑中一街 66 號",
        "twd97lon": "121.458",
        "twd97lat": "25.003",
        "sitetype": "一般測站",
        "siteid": "3",
    }
    row.update(over)
    return row


# ── the coordinate guard ─────────────────────────────────────────────────────


def test_a_real_row_becomes_the_project_vocabulary() -> None:
    frame = _to_frame([record()])

    assert frame.height == 1
    assert frame["station_name"][0] == "板橋"
    assert frame["lon"][0] == pytest.approx(121.458)
    assert frame["lat"][0] == pytest.approx(25.003)
    # The register's own type and zone are suffixed, because the project has its
    # own values for both and the two do not always agree.
    assert "station_type_official" in frame.columns
    assert "airzone_official" in frame.columns


def test_null_island_is_refused() -> None:
    """(0, 0) is the classic one: it plots in the Gulf of Guinea and looks real."""
    with pytest.raises(RuntimeError, match="outside Taiwan"):
        _to_frame([record(twd97lon="0", twd97lat="0")])


def test_a_swapped_pair_is_refused() -> None:
    """25.003, 121.458 is a plausible-looking pair somewhere near Kashmir."""
    with pytest.raises(RuntimeError, match="outside Taiwan"):
        _to_frame([record(twd97lon="25.003", twd97lat="121.458")])


def test_a_decimal_slip_is_refused() -> None:
    """121.458 → 12.1458 is one keystroke and lands the station in Indonesia."""
    with pytest.raises(RuntimeError, match="outside Taiwan"):
        _to_frame([record(twd97lon="12.1458", twd97lat="25.003")])


def test_a_missing_coordinate_is_refused_rather_than_dropped() -> None:
    """Silently dropping it would take a station off the map with no record.

    The module's rule is that a station without coordinates is counted in the
    open. That accounting happens in `reconcile_with_store`; what must not
    happen is a register row arriving here empty and being quietly tolerated.
    """
    with pytest.raises(RuntimeError, match="outside Taiwan"):
        _to_frame([record(twd97lon="", twd97lat="")])


@pytest.mark.parametrize(
    ("name", "lon", "lat"),
    [
        ("馬祖", 119.95, 26.16),
        ("蘭嶼", 121.55, 22.04),
        ("恆春", 120.75, 22.00),
    ],
)
def test_the_bounds_admit_the_outlying_islands(name: str, lon: float, lat: float) -> None:
    """The bounds exist to catch mistakes, not to redraw the country.

    Matsu and Lanyu are the two that a tighter box would exclude, and both have
    monitoring stations. A guard that rejects real places gets loosened by
    whoever hits it next, and then it guards nothing.
    """
    frame = _to_frame([record(sitename=name, twd97lon=str(lon), twd97lat=str(lat))])
    assert frame["station_name"][0] == name


def test_the_bounds_are_not_a_rectangle_around_the_whole_region() -> None:
    """A box wide enough to be useless would pass every test above."""
    span_lon = TAIWAN_BOUNDS["lon_max"] - TAIWAN_BOUNDS["lon_min"]
    span_lat = TAIWAN_BOUNDS["lat_max"] - TAIWAN_BOUNDS["lat_min"]
    assert span_lon < 5.0, f"longitude span {span_lon} is too permissive to catch a slip"
    assert span_lat < 5.0, f"latitude span {span_lat} is too permissive to catch a slip"


# ── the upstream contract ────────────────────────────────────────────────────


def test_a_renamed_upstream_field_fails_loudly() -> None:
    """The register is someone else's table and can be reshaped without notice.

    Losing `twd97lon` silently would leave every station without a coordinate,
    which downstream looks identical to "these stations have no coordinates" —
    a real and expected condition. The two must not be confusable.
    """
    row = record()
    row["longitude"] = row.pop("twd97lon")
    with pytest.raises(RuntimeError, match="twd97lon"):
        _to_frame([row])


@respx.mock
def test_fetch_reads_the_wrapped_shape() -> None:
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, json={"records": [record(), record(sitename="古亭")]})
    )
    frame = fetch_station_register(api_key="KEY")

    assert frame.height == 2
    # Sorted by name, so the order is stable between runs and the committed
    # cache does not churn.
    assert frame["station_name"].to_list() == sorted(frame["station_name"].to_list())


@respx.mock
def test_fetch_refuses_a_register_it_cannot_trust() -> None:
    """A bad coordinate must not reach `conf/station_geo.yaml`.

    The cache is committed, so anything that gets in is published and stays
    published until someone notices a dot in the sea.
    """
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, json={"records": [record(twd97lat="0")]})
    )
    with pytest.raises(RuntimeError, match="outside Taiwan"):
        fetch_station_register(api_key="KEY")


@respx.mock
def test_fetch_says_so_when_the_platform_returns_an_error_body() -> None:
    """MOENV answers an unactivated key with a 200 and a message."""
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, json={"message": "API key not activated"})
    )
    with pytest.raises((RuntimeError, KeyError, pl.exceptions.PolarsError)):
        fetch_station_register(api_key="KEY")


# ── the reconciliation, which is the module's stated ethic ───────────────────


def names(frame: pl.DataFrame, presence: str) -> list[str]:
    return sorted(frame.filter(pl.col("presence") == presence)["station_name"].to_list())


def test_a_station_in_neither_direction_is_kept_and_labelled() -> None:
    """「deliberately a table rather than a filter」, executed.

    A decommissioned station has four decades of measurements and no
    coordinates; a newly commissioned one has coordinates and no measurements.
    Both are real, both are findings about the completeness of Taiwan's open
    record, and a join that dropped either would turn a finding into a silence.
    """
    archive = pl.DataFrame({"station_name": ["板橋", "古亭", "三重"]})
    register = pl.DataFrame({"station_name": ["板橋", "古亭", "麥寮"]})

    out = reconcile_with_store(archive, register)

    assert out.height == 4, "a station present on only one side was dropped"
    assert names(out, "both") == ["古亭", "板橋"]
    assert names(out, "archive_only") == ["三重"], "measured but unmappable"
    assert names(out, "register_only") == ["麥寮"], "mappable but unmeasured"


def test_every_row_gets_exactly_one_verdict() -> None:
    archive = pl.DataFrame({"station_name": ["板橋", "三重"]})
    register = pl.DataFrame({"station_name": ["板橋", "麥寮"]})

    out = reconcile_with_store(archive, register)

    assert out["presence"].null_count() == 0
    assert set(out["presence"].to_list()) <= {"both", "archive_only", "register_only"}
    assert out["station_name"].n_unique() == out.height


def test_an_empty_register_does_not_erase_the_archive() -> None:
    """The failure mode where an upstream outage silently empties the map.

    If the register came back empty and this returned nothing, the site would
    render a map with no stations and no error — which reads as "there are no
    stations" rather than "the register could not be read".
    """
    archive = pl.DataFrame({"station_name": ["板橋", "古亭"]})
    out = reconcile_with_store(
        archive, pl.DataFrame({"station_name": []}, schema={"station_name": pl.Utf8})
    )

    assert out.height == 2
    assert names(out, "archive_only") == ["古亭", "板橋"]
    assert names(out, "both") == []
