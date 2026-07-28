"""Station geography, from MOENV's own station register (dataset ``aqx_p_07``).

Everything else in this project is derived from the archives themselves. The
archives carry no coordinates, so the map, the county rollups and the "where
you live" chapter of the site all depend on this one external table.

Two design decisions follow from that dependency:

**The result is cached into ``conf/station_geo.yaml`` and committed.** Building
the site must not require an API key, and a published figure must not silently
change because an upstream register was edited between two runs. Refreshing is
an explicit act (``twair stations geo --refresh``) with a visible diff.

**The register and the archives are reconciled, not merged.** The register
describes the stations that exist *now*; the archives describe the stations
that existed *then*. They disagree in both directions — decommissioned
stations have four decades of measurements and no coordinates, and newly
commissioned stations have coordinates and no measurements. Both gaps are
recorded as data. A station without coordinates is drawn nowhere and counted
in the open, never given a plausible-looking guess.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import polars as pl

from twair.config import ConfigError, get_settings, load_conf, write_conf
from twair.net import PoliteClient, quiet_http
from twair.store.stations import normalise_name_expr

log = logging.getLogger(__name__)

__all__ = [
    "TAIWAN_BOUNDS",
    "fetch_station_register",
    "load_station_geo",
    "reconcile_with_store",
    "refresh_station_geo",
]

API_URL = "https://data.moenv.gov.tw/api/v2/aqx_p_07"
CONF_NAME = "station_geo"

# Generous enough for Matsu (26.3N, 119.9E) and Lanyu (22.0N, 121.6E), tight
# enough that a swapped lat/lon pair or a zero fails loudly instead of putting
# a monitoring station in the Pacific.
TAIWAN_BOUNDS = {"lon_min": 118.0, "lon_max": 122.2, "lat_min": 21.7, "lat_max": 26.5}

# The register's field names, mapped onto this project's vocabulary. The
# official type and zone are suffixed ``_official`` because the project has its
# own values for both, derived from the 1994 ReadMe and from archive paths, and
# the two do not always agree — see ``reconcile_with_store``.
FIELDS = {
    "sitename": "station_name",
    "siteengname": "station_name_en",
    "areaname": "airzone_official",
    "county": "county",
    "township": "township",
    "siteaddress": "address",
    "twd97lon": "lon",
    "twd97lat": "lat",
    "sitetype": "station_type_official",
    "siteid": "site_id",
}


def fetch_station_register(*, api_key: str | None = None, limit: int = 1000) -> pl.DataFrame:
    """Download the register. Requires ``MOENV_API_KEY``.

    The key travels as a query parameter, so the request is made inside
    :func:`~twair.net.quiet_http`.
    """
    key = api_key or get_settings().require("moenv_api_key")

    with quiet_http(), PoliteClient() as client:
        payload = client.get_json(
            API_URL, params={"api_key": key, "limit": limit, "format": "json"}
        )

    # The v2 endpoint returns a bare list for this dataset and a
    # ``{"records": [...]}`` envelope for others. Accept either rather than
    # depending on which one today's deployment happens to serve.
    records = payload.get("records", []) if isinstance(payload, dict) else payload
    if not records:
        raise RuntimeError(f"{API_URL} returned no station records")

    return _to_frame(records)


def _to_frame(records: list[dict[str, Any]]) -> pl.DataFrame:
    """Shape raw register records, normalise names, and validate coordinates."""
    frame = pl.DataFrame(records, infer_schema_length=None)

    missing = set(FIELDS) - set(frame.columns)
    if missing:
        raise RuntimeError(
            f"station register is missing expected field(s) {sorted(missing)}; "
            f"the upstream schema has changed"
        )

    frame = (
        frame.select(*(pl.col(src).cast(pl.Utf8).alias(dst) for src, dst in FIELDS.items()))
        .with_columns(
            normalise_name_expr(),
            pl.col("lon").cast(pl.Float64, strict=False),
            pl.col("lat").cast(pl.Float64, strict=False),
        )
        .sort("station_name")
    )

    _validate_coordinates(frame)
    return frame


def _validate_coordinates(frame: pl.DataFrame) -> None:
    """Fail on any coordinate outside Taiwan rather than plotting it.

    A null island (0, 0), a swapped pair, or a decimal-place slip all render as
    a station somewhere in the ocean, which on a map reads as a real place. The
    map cannot check this; here is where it gets checked.
    """
    bad = frame.filter(
        pl.col("lon").is_null()
        | pl.col("lat").is_null()
        | ~pl.col("lon").is_between(TAIWAN_BOUNDS["lon_min"], TAIWAN_BOUNDS["lon_max"])
        | ~pl.col("lat").is_between(TAIWAN_BOUNDS["lat_min"], TAIWAN_BOUNDS["lat_max"])
    )
    if not bad.is_empty():
        offenders = bad.select("station_name", "lon", "lat").to_dicts()
        raise RuntimeError(f"station coordinates outside Taiwan: {offenders}")


def refresh_station_geo(*, api_key: str | None = None) -> pl.DataFrame:
    """Fetch the register and write ``conf/station_geo.yaml``."""
    frame = fetch_station_register(api_key=api_key)

    write_conf(
        CONF_NAME,
        {
            "source": API_URL,
            "dataset": "aqx_p_07 空氣品質監測站基本資料",
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "count": frame.height,
            "stations": frame.to_dicts(),
        },
        header=(
            "測站座標與行政區 —— 由環境部開放資料平臺 aqx_p_07 取得，GENERATED FILE。\n"
            "\n"
            "重新產生：uv run twair stations geo --refresh\n"
            "\n"
            "這份檔案刻意進版控：網站建置不應該需要 API key，\n"
            "已發布的圖表也不該因為上游在兩次執行之間改了資料而悄悄變動。\n"
            "\n"
            "此登錄只涵蓋『現存』測站。已停用的測站有觀測資料卻沒有座標，\n"
            "新設的測站有座標卻沒有觀測資料。兩種缺口都如實記錄，不做補值。"
        ),
    )
    log.info("wrote %d stations to conf/%s.yaml", frame.height, CONF_NAME)
    return frame


def load_station_geo() -> pl.DataFrame:
    """Read the cached register. No network, no credential.

    Returns an empty frame with the right schema when the cache is absent, so
    that every downstream join degrades to null coordinates rather than
    raising. The absence is then visible in the output as a station that cannot
    be mapped, which is the honest rendering of "we do not know where this is".
    """
    empty = pl.DataFrame(
        schema={
            "station_name": pl.Utf8,
            "station_name_en": pl.Utf8,
            "airzone_official": pl.Utf8,
            "county": pl.Utf8,
            "township": pl.Utf8,
            "address": pl.Utf8,
            "lon": pl.Float64,
            "lat": pl.Float64,
            "station_type_official": pl.Utf8,
            "site_id": pl.Utf8,
        }
    )

    try:
        conf = load_conf(CONF_NAME)
    except FileNotFoundError:
        log.warning(
            "conf/%s.yaml not found — stations will have no coordinates. "
            "Run `twair stations geo --refresh` with MOENV_API_KEY set.",
            CONF_NAME,
        )
        return empty

    stations = conf.get("stations") or []
    if not stations:
        raise ConfigError(f"conf/{CONF_NAME}.yaml has no `stations`")

    return pl.DataFrame(stations, schema=empty.schema)


def reconcile_with_store(
    store_stations: pl.DataFrame, geo: pl.DataFrame | None = None
) -> pl.DataFrame:
    """Compare the register against the stations the archives actually contain.

    One row per station name from either side, with ``presence`` in:

    * ``both`` — measured and mappable;
    * ``archive_only`` — decommissioned, or renamed beyond the alias table.
      It has data and no coordinates;
    * ``register_only`` — commissioned too recently to appear in any annual
      archive. It has coordinates and no data.

    This is deliberately a table rather than a filter. "Six stations with four
    decades of measurements cannot be placed on a map" is a finding about the
    completeness of Taiwan's open air-quality record, and it belongs in the
    documentation, not in a dropped row.
    """
    register = geo if geo is not None else load_station_geo()

    left = store_stations.select("station_name").with_columns(pl.lit(True).alias("_in_archive"))
    right = register.select("station_name").with_columns(pl.lit(True).alias("_in_register"))

    return (
        left.join(right, on="station_name", how="full", coalesce=True)
        .with_columns(
            pl.col("_in_archive").fill_null(False),
            pl.col("_in_register").fill_null(False),
        )
        .with_columns(
            pl.when(pl.col("_in_archive") & pl.col("_in_register"))
            .then(pl.lit("both"))
            .when(pl.col("_in_archive"))
            .then(pl.lit("archive_only"))
            .otherwise(pl.lit("register_only"))
            .alias("presence")
        )
        .select("station_name", "presence")
        .sort("presence", "station_name")
    )
