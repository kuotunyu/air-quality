"""ERA5 acquisition keeps the provider grid, units, keys, and nulls explicit."""

from __future__ import annotations

import calendar
import json
import shutil
import warnings
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import netCDF4
import numpy as np
import polars as pl
import pytest

from twair.config import ConfigError
from twair.ingest.era5 import (
    CdsEra5Backend,
    acquire_era5,
    build_era5_request,
    load_era5_source,
    read_era5_grid,
    read_era5_result,
    sample_era5_stations,
)
from twair.ingest.station_inventory import station_inventory_generation

# netCDF4 1.7 writes fixture cells through NumPy's deprecated shape setter;
# production code only reads these files, so the warning does not describe it.
pytestmark = pytest.mark.filterwarnings(
    "ignore:Setting the shape on a NumPy array has been deprecated:DeprecationWarning"
)

_OBSERVED_VALUES = {
    "blh": np.array(
        [
            [[39.63911437988281, 58.63911437988281], [45.13911437988281, 67.45161437988281]],
            [[235.40240478515625, 261.65240478515625], [261.90240478515625, 288.40240478515625]],
        ],
        dtype=np.float32,
    ),
    "u10": np.array(
        [
            [
                [-0.6209869384765625, -1.0155181884765625],
                [-0.6561431884765625, -1.1043853759765625],
            ],
            [[-0.760528564453125, -0.920684814453125], [-0.955841064453125, -1.108184814453125]],
        ],
        dtype=np.float32,
    ),
    "v10": np.array(
        [
            [[0.9363861083984375, 0.6815032958984375], [0.8328704833984375, 0.3592376708984375]],
            [[0.384765625, 0.4677734375], [0.21875, 0.1494140625]],
        ],
        dtype=np.float32,
    ),
    "t2m": np.array(
        [
            [[277.92578125, 277.90234375], [277.630859375, 277.7578125]],
            [[278.844482421875, 278.471435546875], [278.526123046875, 278.155029296875]],
        ],
        dtype=np.float32,
    ),
    "d2m": np.array(
        [
            [[277.130126953125, 277.735595703125], [276.694580078125, 277.102783203125]],
            [[276.630615234375, 276.882568359375], [276.023193359375, 276.284912109375]],
        ],
        dtype=np.float32,
    ),
    "sp": np.array(
        [
            [[97614.9375, 97865.9375], [96364.9375, 96860.9375]],
            [[97627.0, 97875.0], [96376.0, 96869.0]],
        ],
        dtype=np.float32,
    ),
}

_UNITS = {
    "blh": "m",
    "u10": "m s**-1",
    "v10": "m s**-1",
    "t2m": "K",
    "d2m": "K",
    "sp": "Pa",
}


def _write_observed_fixture(
    path: Path,
    *,
    omit: str | None = None,
    unit_override: tuple[str, str] | None = None,
    duplicate_time: bool = False,
    mask_first_blh: bool = False,
) -> None:
    with netCDF4.Dataset(path, "w", format="NETCDF4") as dataset:
        dataset.Conventions = "CF-1.7"
        dataset.createDimension("valid_time", 2)
        dataset.createDimension("latitude", 2)
        dataset.createDimension("longitude", 2)

        valid_time = dataset.createVariable("valid_time", "i8", ("valid_time",))
        valid_time.units = "seconds since 1970-01-01"
        valid_time.calendar = "proleptic_gregorian"
        valid_time.standard_name = "time"
        valid_time[:] = [1735689600, 1735689600 if duplicate_time else 1735693200]

        latitude = dataset.createVariable("latitude", "f8", ("latitude",))
        latitude.units = "degrees_north"
        latitude.stored_direction = "decreasing"
        latitude[:] = [26.5, 26.25]

        longitude = dataset.createVariable("longitude", "f8", ("longitude",))
        longitude.units = "degrees_east"
        longitude[:] = [118.0, 118.25]

        number = dataset.createVariable("number", "i8")
        number.assignValue(0)
        expver = dataset.createVariable("expver", str, ("valid_time",))
        expver[0] = "0001"
        expver[1] = "0001"

        for name, values in _OBSERVED_VALUES.items():
            if name == omit:
                continue
            variable = dataset.createVariable(
                name,
                "f4",
                ("valid_time", "latitude", "longitude"),
                fill_value=np.nan,
            )
            variable.units = (
                unit_override[1]
                if unit_override is not None and unit_override[0] == name
                else _UNITS[name]
            )
            copied = values.copy()
            if name == "blh" and mask_first_blh:
                copied[0, 0, 0] = np.nan
            for index in np.ndindex(copied.shape):
                variable[index] = copied[index]


def _write_complete_month_fixture(
    path: Path,
    *,
    year: int,
    month: int,
    mask_first_blh: bool = True,
) -> None:
    n_hours = calendar.monthrange(year, month)[1] * 24
    first_hour = datetime(year, month, 1, tzinfo=UTC)
    timestamps = np.arange(
        int(first_hour.timestamp()),
        int(first_hour.timestamp()) + n_hours * 3600,
        3600,
        dtype=np.int64,
    )
    with netCDF4.Dataset(path, "w", format="NETCDF4") as dataset:
        dataset.Conventions = "CF-1.7"
        dataset.createDimension("valid_time", n_hours)
        dataset.createDimension("latitude", 2)
        dataset.createDimension("longitude", 2)

        valid_time = dataset.createVariable("valid_time", "i8", ("valid_time",))
        valid_time.units = "seconds since 1970-01-01"
        valid_time.calendar = "proleptic_gregorian"
        valid_time.standard_name = "time"
        valid_time[:] = timestamps

        latitude = dataset.createVariable("latitude", "f8", ("latitude",))
        latitude.units = "degrees_north"
        latitude[:] = [26.5, 26.25]
        longitude = dataset.createVariable("longitude", "f8", ("longitude",))
        longitude.units = "degrees_east"
        longitude[:] = [118.0, 118.25]

        repetitions = (n_hours + 1) // 2
        for name, observed in _OBSERVED_VALUES.items():
            variable = dataset.createVariable(
                name,
                "f4",
                ("valid_time", "latitude", "longitude"),
                fill_value=np.nan,
            )
            variable.units = _UNITS[name]
            values = np.tile(observed, (repetitions, 1, 1))[:n_hours]
            if name == "blh" and mask_first_blh:
                values[0, 0, 0] = np.nan
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="Setting the shape on a NumPy array has been deprecated",
                    category=DeprecationWarning,
                )
                variable[:] = values


def _stations() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": ["西北格點", "東南格點"],
            "lon": [118.02, 118.24],
            "lat": [26.48, 26.26],
        }
    )


@dataclass
class FakeEra5Backend:
    calls: list[int] = field(default_factory=list)
    fail_month: int | None = None

    def retrieve(
        self,
        dataset: str,
        request: dict[str, object],
        target: Path,
    ) -> None:
        assert dataset == "reanalysis-era5-single-levels"
        years = request["year"]
        months = request["month"]
        assert isinstance(years, list)
        assert isinstance(months, list)
        year = int(years[0])
        month = int(months[0])
        self.calls.append(month)
        if month == self.fail_month:
            raise RuntimeError("injected CDS failure")
        _write_complete_month_fixture(target, year=year, month=month)


def _generation_destination(tmp_path: Path) -> Path:
    generation = station_inventory_generation(_stations()).sha256
    return tmp_path / "era5" / "generations" / generation / "year=2025"


def test_era5_config_names_only_the_measured_instant_variables() -> None:
    source = load_era5_source()

    assert source.dataset == "reanalysis-era5-single-levels"
    assert source.area == (26.5, 118.0, 21.75, 122.0)
    assert [(item.output_name, item.netcdf_name, item.unit) for item in source.variables] == [
        ("blh_m", "blh", "m"),
        ("u10_m_s", "u10", "m s**-1"),
        ("v10_m_s", "v10", "m s**-1"),
        ("t2m_k", "t2m", "K"),
        ("d2m_k", "d2m", "K"),
        ("sp_pa", "sp", "Pa"),
    ]
    assert all(item.request_name != "total_precipitation" for item in source.variables)


def test_era5_config_rejects_an_inverted_area() -> None:
    config = {
        "single_levels": {
            "dataset": "reanalysis-era5-single-levels",
            "product_type": "reanalysis",
            "data_format": "netcdf",
            "download_format": "unarchived",
            "area": [21.75, 118.0, 26.5, 122.0],
            "variables": {},
        }
    }

    with pytest.raises(ConfigError, match="area"):
        load_era5_source(config)


def test_month_request_uses_every_hour_and_no_accumulated_variable() -> None:
    request = build_era5_request(load_era5_source(), year=2025, month=1)

    assert request["year"] == ["2025"]
    assert request["month"] == ["01"]
    assert request["day"] == [f"{day:02d}" for day in range(1, 32)]
    assert request["time"] == [f"{hour:02d}:00" for hour in range(24)]
    assert request["area"] == [26.5, 118.0, 21.75, 122.0]
    assert request["data_format"] == "netcdf"
    assert request["download_format"] == "unarchived"
    assert "total_precipitation" not in request["variable"]


def test_real_valid_time_schema_is_read_from_bytes_below_a_unicode_path(tmp_path: Path) -> None:
    source_path = tmp_path / "observed.nc"
    _write_observed_fixture(source_path)
    unicode_dir = tmp_path / "資料"
    unicode_dir.mkdir()
    unicode_path = unicode_dir / "era5.nc"
    shutil.copyfile(source_path, unicode_path)

    grid = read_era5_grid(unicode_path, load_era5_source(), year=2025, month=1)

    assert grid.times == (
        datetime(2025, 1, 1, 0, tzinfo=UTC),
        datetime(2025, 1, 1, 1, tzinfo=UTC),
    )
    assert grid.latitudes.tolist() == [26.5, 26.25]
    assert grid.longitudes.tolist() == [118.0, 118.25]
    assert grid.values["blh_m"].shape == (2, 2, 2)


def test_reader_rejects_a_missing_scientific_variable(tmp_path: Path) -> None:
    path = tmp_path / "missing.nc"
    _write_observed_fixture(path, omit="blh")

    with pytest.raises(RuntimeError, match="blh"):
        read_era5_grid(path, load_era5_source(), year=2025, month=1)


def test_reader_rejects_provider_unit_drift(tmp_path: Path) -> None:
    path = tmp_path / "unit-drift.nc"
    _write_observed_fixture(path, unit_override=("blh", "km"))

    with pytest.raises(RuntimeError, match="unit"):
        read_era5_grid(path, load_era5_source(), year=2025, month=1)


def test_reader_rejects_duplicate_hour_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-time.nc"
    _write_observed_fixture(path, duplicate_time=True)

    with pytest.raises(RuntimeError, match="hour"):
        read_era5_grid(path, load_era5_source(), year=2025, month=1)


def test_station_sampling_preserves_every_key_and_a_masked_source_value(tmp_path: Path) -> None:
    path = tmp_path / "masked.nc"
    _write_observed_fixture(path, mask_first_blh=True)
    grid = read_era5_grid(path, load_era5_source(), year=2025, month=1)
    stations = pl.DataFrame(
        {
            "station_name": ["西北格點", "東南格點"],
            "lon": [118.02, 118.24],
            "lat": [26.48, 26.26],
        }
    )

    sampled = sample_era5_stations(grid, stations)

    assert sampled.height == 4
    assert sampled.unique(["station_name", "ts_utc"]).height == 4
    assert sampled.schema["ts_utc"] == pl.Datetime("us", "UTC")
    assert (
        sampled.filter(
            (pl.col("station_name") == "西北格點")
            & (pl.col("ts_utc") == datetime(2025, 1, 1, 0, tzinfo=UTC))
        )["blh_m"].item()
        is None
    )
    assert sampled.filter(pl.col("station_name") == "東南格點").select(
        pl.col("grid_lat").unique(),
        pl.col("grid_lon").unique(),
    ).to_dicts() == [{"grid_lat": 26.25, "grid_lon": 118.25}]
    assert sampled["grid_distance_km"].is_not_null().all()


def test_acquisition_refuses_remote_access_without_both_explicit_guards(tmp_path: Path) -> None:
    backend = FakeEra5Backend()
    destination = _generation_destination(tmp_path)

    with pytest.raises(RuntimeError, match="confirm-download"):
        acquire_era5(
            _stations(),
            backend=backend,
            year=2025,
            months=(1,),
            inventory_generation=True,
            confirm_download=False,
            destination=destination,
        )
    with pytest.raises(RuntimeError, match="inventory-generation"):
        acquire_era5(
            _stations(),
            backend=backend,
            year=2025,
            months=(1,),
            inventory_generation=False,
            confirm_download=True,
            destination=destination,
        )

    assert backend.calls == []
    assert not destination.exists()


def test_acquisition_refuses_a_destination_for_another_inventory_generation(
    tmp_path: Path,
) -> None:
    backend = FakeEra5Backend()
    wrong = tmp_path / "era5" / "generations" / ("0" * 64) / "year=2025"

    with pytest.raises(RuntimeError, match="destination generation"):
        acquire_era5(
            _stations(),
            backend=backend,
            year=2025,
            months=(1,),
            inventory_generation=True,
            confirm_download=True,
            destination=wrong,
        )

    assert backend.calls == []
    assert not wrong.exists()


def test_acquisition_rejects_a_request_area_that_excludes_a_reviewed_station(
    tmp_path: Path,
) -> None:
    backend = FakeEra5Backend()
    source = replace(load_era5_source(), area=(26.5, 118.0, 26.4, 118.5))

    with pytest.raises(RuntimeError, match="outside the ERA5 request area"):
        acquire_era5(
            _stations(),
            backend=backend,
            year=2025,
            months=(1,),
            inventory_generation=True,
            confirm_download=True,
            destination=_generation_destination(tmp_path),
            source=source,
        )

    assert backend.calls == []


def test_cds_adapter_passes_the_exact_dataset_request_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cdsapi

    observed: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            observed["client"] = kwargs

        def retrieve(
            self,
            dataset: str,
            request: dict[str, object],
            target: str,
        ) -> None:
            observed["retrieve"] = (dataset, request, target)

    monkeypatch.setattr(cdsapi, "Client", FakeClient)
    source = load_era5_source()
    request = build_era5_request(source, year=2025, month=1)
    target = tmp_path / "month=01.nc"

    CdsEra5Backend("https://cds.example/api", "secret").retrieve(
        source.dataset,
        request,
        target,
    )

    assert observed["client"] == {
        "url": "https://cds.example/api",
        "key": "secret",
        "quiet": True,
        "progress": False,
    }
    assert observed["retrieve"] == (source.dataset, request, str(target))


def test_generation_result_records_raw_identity_coverage_and_source_contract(
    tmp_path: Path,
) -> None:
    backend = FakeEra5Backend()
    destination = _generation_destination(tmp_path)
    generation = station_inventory_generation(_stations()).sha256

    result = acquire_era5(
        _stations(),
        backend=backend,
        year=2025,
        months=(1,),
        inventory_generation=True,
        confirm_download=True,
        destination=destination,
        generated_at="2026-08-11T00:00:00+00:00",
    )

    raw_path = destination / "raw" / "month=01.nc"
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert backend.calls == [1]
    assert result.values.height == 2 * 31 * 24
    assert result.values.unique(["station_name", "ts_utc"]).height == result.values.height
    assert result.values["blh_m"].null_count() == 1
    assert result.coverage.height == 6
    assert manifest["inventory_generation_sha256"] == generation
    assert manifest["months"] == [1]
    assert manifest["rows"] == result.values.height
    assert manifest["raw_files"]["01"]["sha256"] == sha256(raw_path.read_bytes()).hexdigest()
    assert manifest["raw_files"]["01"]["request"] == build_era5_request(
        load_era5_source(), year=2025, month=1
    )
    assert read_era5_result(destination).values.equals(result.values)


def test_a_validated_raw_month_is_reused_without_contacting_cds(tmp_path: Path) -> None:
    destination = _generation_destination(tmp_path)
    acquire_era5(
        _stations(),
        backend=FakeEra5Backend(),
        year=2025,
        months=(1,),
        inventory_generation=True,
        confirm_download=True,
        destination=destination,
    )
    first_raw = (destination / "raw" / "month=01.nc").read_bytes()
    backend = FakeEra5Backend()

    result = acquire_era5(
        _stations(),
        backend=backend,
        year=2025,
        months=(1,),
        inventory_generation=True,
        confirm_download=True,
        destination=destination,
    )

    assert backend.calls == []
    assert (destination / "raw" / "month=01.nc").read_bytes() == first_raw
    assert result.manifest["months"] == [1]


def test_a_new_month_is_added_without_erasing_the_validated_first_month(tmp_path: Path) -> None:
    destination = _generation_destination(tmp_path)
    acquire_era5(
        _stations(),
        backend=FakeEra5Backend(),
        year=2025,
        months=(1,),
        inventory_generation=True,
        confirm_download=True,
        destination=destination,
    )
    backend = FakeEra5Backend()

    result = acquire_era5(
        _stations(),
        backend=backend,
        year=2025,
        months=(7,),
        inventory_generation=True,
        confirm_download=True,
        destination=destination,
    )

    assert backend.calls == [7]
    assert result.manifest["months"] == [1, 7]
    assert result.values.height == 2 * (31 + 31) * 24
    assert (destination / "raw" / "month=01.nc").is_file()
    assert (destination / "raw" / "month=07.nc").is_file()


def test_a_changed_raw_file_is_refused_instead_of_silently_reused(tmp_path: Path) -> None:
    destination = _generation_destination(tmp_path)
    acquire_era5(
        _stations(),
        backend=FakeEra5Backend(),
        year=2025,
        months=(1,),
        inventory_generation=True,
        confirm_download=True,
        destination=destination,
    )
    raw_path = destination / "raw" / "month=01.nc"
    raw_path.write_bytes(raw_path.read_bytes() + b"changed")
    backend = FakeEra5Backend()

    with pytest.raises(RuntimeError, match="checksum"):
        acquire_era5(
            _stations(),
            backend=backend,
            year=2025,
            months=(1,),
            inventory_generation=True,
            confirm_download=True,
            destination=destination,
        )

    assert backend.calls == []


def test_a_failed_new_month_download_leaves_the_previous_generation_unchanged(
    tmp_path: Path,
) -> None:
    destination = _generation_destination(tmp_path)
    acquire_era5(
        _stations(),
        backend=FakeEra5Backend(),
        year=2025,
        months=(1,),
        inventory_generation=True,
        confirm_download=True,
        destination=destination,
    )
    before = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }

    with pytest.raises(RuntimeError, match="injected CDS failure"):
        acquire_era5(
            _stations(),
            backend=FakeEra5Backend(fail_month=7),
            year=2025,
            months=(7,),
            inventory_generation=True,
            confirm_download=True,
            destination=destination,
        )

    after = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not list(destination.parent.glob(f".{destination.name}.staging-*"))
