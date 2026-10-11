"""Satellite surface fields from ERDDAP: query construction, axis normalization, the multi-sensor composite (most
recent valid pixel and its age), the coastal mask, and cache pruning. No network: a fake `requests.get` serves
NetCDF-3 shaped like ERDDAP griddap responses."""

import io
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from forcingkit.fetchers import coastwatch

BBOX = [-73.0, 41.0, -72.97, 41.03]
LAT_DESC = np.array(
    [41.03, 41.0225, 41.015, 41.0075, 41.0]
)  # descending, as the VIIRS sectors are
LON = np.array([-73.0, -72.9925, -72.985, -72.9775, -72.97])


def griddap_nc(
    var: str, times: list[str], values: np.ndarray, altitude: bool = True
) -> bytes:
    """NetCDF-3 bytes like an ERDDAP griddap .nc response (time, [altitude], latitude, longitude)."""
    dims = (
        ("time", "altitude", "latitude", "longitude")
        if altitude
        else ("time", "latitude", "longitude")
    )
    data = values[:, None] if altitude else values
    # ERDDAP writes CF times (seconds since 1970, UTC), which decode as naive UTC.
    coords = {
        "time": pd.DatetimeIndex(times).tz_convert(None),
        "latitude": LAT_DESC,
        "longitude": LON,
    }
    if altitude:
        coords["altitude"] = [0.0]
    ds = xr.Dataset(
        {var: (dims, data.astype(np.float32), {"units": "mg m^-3"})}, coords=coords
    )
    buf = io.BytesIO()
    ds.to_netcdf(buf, engine="scipy")
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status: int, content: bytes = b"", text: str = ""):
        self.status_code = status
        self.content = content
        self.text = text or content.decode("latin-1", errors="ignore")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeERDDAP:
    """Serves `time[(last)]` and griddap subsets per dataset from canned arrays."""

    def __init__(self, data: dict[str, tuple[list[str], np.ndarray]]):
        self.data = data
        self.urls: list[str] = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        ds_id = url.split("/griddap/")[1].split(".")[0]
        if ds_id not in self.data:
            return FakeResponse(
                404, text="Error: Your query produced no matching results."
            )
        times, values = self.data[ds_id]
        if "time[(last)]" in url:
            return FakeResponse(200, text=f"time\nUTC\n{times[-1]}\n")
        if "time[0]" in url:
            return FakeResponse(200, text=f"time\nUTC\n{times[0]}\n")
        var = url.split("?")[1].split("[")[0]
        return FakeResponse(200, content=griddap_nc(var, times, values))


@pytest.fixture
def erddap(monkeypatch):
    def install(data):
        fake = FakeERDDAP(data)
        monkeypatch.setattr(coastwatch.requests, "get", fake)
        return fake

    return install


def test_griddap_url_orders_descending_latitude_and_altitude():
    p = coastwatch.PRODUCTS["chl_viirs_snpp"]
    url = coastwatch.griddap_url(
        p,
        BBOX,
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    assert url.startswith(
        "https://coastwatch.noaa.gov/erddap/griddap/noaacwNPPVIIRSchlaSectorVYDaily.nc?chlor_a"
    )
    assert "[(0.0)][(41.03):(41.0)][(-73.0):(-72.97)]" in url
    mur = coastwatch.griddap_url(
        coastwatch.PRODUCTS["sst_mur"],
        BBOX,
        datetime(2026, 10, 1),
        datetime(2026, 10, 2),
    )
    assert "[(41.0):(41.03)]" in mur and "(0.0)" not in mur


def test_fetch_normalizes_axes_and_trims_to_latest(erddap):
    vals = np.arange(2 * 5 * 5, dtype=float).reshape(2, 5, 5)
    fake = erddap(
        {
            "noaacwNPPVIIRSchlaSectorVYDaily": (
                ["2026-10-05T18:00:00Z", "2026-10-06T18:00:00Z"],
                vals,
            )
        }
    )
    ds = coastwatch.fetch(
        "chl_viirs_snpp",
        BBOX,
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 9, tzinfo=timezone.utc),
    )
    assert ds["chlor_a"].dims == ("time", "lat", "lon")
    assert ds["chlor_a"].dtype == np.float32
    assert np.all(np.diff(ds["lat"].values) > 0)  # ascending
    assert ds.attrs["attribution"] == coastwatch.NOAA_CREDIT
    # The window was trimmed to the latest time step before the subset request.
    assert "2026-10-06T18:00:00Z)]" in fake.urls[-1]


def test_fetch_missing_dataset_returns_empty(erddap):
    erddap({})
    ds = coastwatch.fetch(
        "chl_viirs_n20", BBOX, datetime(2026, 10, 1), datetime(2026, 10, 2)
    )
    assert ds.sizes["time"] == 0


def test_composite_keeps_most_recent_valid_pixel_with_age(erddap):
    nan = np.nan
    older = np.full((1, 5, 5), 1.0)
    newer = np.full((1, 5, 5), 2.0)
    newer[0, 0, 0] = nan  # cloud: the older value should show through here
    erddap(
        {
            "noaacwNPPVIIRSchlaSectorVYDaily": (["2026-10-05T12:00:00Z"], older),
            "noaacwN20VIIRSchlaSectorUSDaily": (["2026-10-07T12:00:00Z"], newer),
        }
    )
    end = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    c = coastwatch.composite(
        ["chl_viirs_snpp", "chl_viirs_n20", "chl_viirs_n21"], BBOX, end, days=7
    )
    value, age, src = c["value"].values, c["age_days"].values, c["product_index"].values
    # Latitude ascends, so the cloudy pixel (first row of the descending input) is the last row here.
    assert (
        value[-1, 0] == pytest.approx(1.0)
        and age[-1, 0] == pytest.approx(3.0)
        and src[-1, 0] == 0
    )
    assert (
        value[0, 0] == pytest.approx(2.0)
        and age[0, 0] == pytest.approx(1.0)
        and src[0, 0] == 1
    )
    assert (
        c.attrs["products"] == "chl_viirs_snpp,chl_viirs_n20"
    )  # the sensor with no data is left out


def test_composite_raises_when_nothing_valid(erddap):
    erddap({})
    with pytest.raises(ValueError):
        coastwatch.composite(
            ["chl_viirs_snpp"], BBOX, datetime(2026, 10, 8, tzinfo=timezone.utc)
        )


def test_mask_coast_erodes_water_next_to_land():
    value = np.ones((5, 5), dtype=np.float32)
    value[:, 0] = np.nan  # a column of land
    ds = xr.Dataset(
        {
            "value": (("lat", "lon"), value),
            "age_days": (("lat", "lon"), np.zeros((5, 5), np.float32)),
            "product_index": (("lat", "lon"), np.zeros((5, 5), np.int8)),
        },
        coords={"lat": np.arange(5.0), "lon": np.arange(5.0)},
    )
    m = coastwatch.mask_coast(ds, pixels=1)
    assert np.isnan(
        m["value"].values[:, 1]
    ).all()  # the water column beside land is masked
    assert np.isfinite(m["value"].values[:, 2:]).all()
    assert (m["product_index"].values[:, 1] == -1).all()


def test_prune_cache_removes_only_old_satellite_stores(tmp_path):
    old = tmp_path / "sat_old.zarr"
    old.mkdir()
    fresh = tmp_path / "sat_new.zarr"
    fresh.mkdir()
    other = tmp_path / "riv_keep.zarr"
    other.mkdir()
    long_ago = time.time() - 40 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    assert coastwatch.prune_cache(str(tmp_path), max_age_days=30) == 1
    assert not old.exists() and fresh.exists() and other.exists()


def test_cache_key_is_stable_within_the_hour():
    a = coastwatch.cache_key(["sst_mur"], BBOX, datetime(2026, 10, 8, 12, 5), 7, 1)
    b = coastwatch.cache_key(["sst_mur"], BBOX, datetime(2026, 10, 8, 12, 55), 7, 1)
    c = coastwatch.cache_key(["sst_mur"], BBOX, datetime(2026, 10, 8, 13, 5), 7, 1)
    assert a == b != c and a.startswith("sat_")


def _three_days():
    """Three daily passes: day 1 clear (1.0), day 2 cloudy in the first row (2.0 elsewhere), day 3 all cloud."""
    v = np.stack(
        [np.full((5, 5), 1.0), np.full((5, 5), 2.0), np.full((5, 5), np.nan)]
    ).astype(np.float32)
    v[1, 0, :] = np.nan
    times = ["2026-10-01T12:00:00Z", "2026-10-02T12:00:00Z", "2026-10-03T12:00:00Z"]
    return times, v


def test_composite_series_rolls_the_window(erddap):
    erddap({"noaacwNPPVIIRSchlaSectorVYDaily": _three_days()})
    ends = [datetime(2026, 10, d, 23, 59, tzinfo=timezone.utc) for d in (1, 2, 3, 5)]
    s = coastwatch.composite_series(["chl_viirs_snpp"], BBOX, ends, days=2)
    assert s["value"].dims == ("time", "lat", "lon") and s.sizes["time"] == 4
    value, age = s["value"].values, s["age_days"].values
    # Latitude ascends, so the descending input's first row is the last row here.
    assert value[0, 0, 0] == pytest.approx(1.0)  # Oct 1: the first pass
    assert value[1, 0, 0] == pytest.approx(2.0) and value[1, -1, 0] == pytest.approx(
        1.0
    )  # Oct 2: cloud keeps day 1
    assert age[1, -1, 0] == pytest.approx(1.0 + 11.98 / 24, abs=1e-3)
    assert value[2, 0, 0] == pytest.approx(
        2.0
    )  # Oct 3 is all cloud: Oct 2 carries over
    assert np.isnan(value[3]).all()  # Oct 5: nothing within 2 days
    assert np.isnan(
        value[2, -1, 0]
    )  # Oct 3: the cloudy row's Oct 1 value is 2.5 days old, outside the window


def test_composite_series_rejects_unsorted_ends(erddap):
    erddap({"noaacwNPPVIIRSchlaSectorVYDaily": _three_days()})
    ends = [
        datetime(2026, 10, 3, tzinfo=timezone.utc),
        datetime(2026, 10, 2, tzinfo=timezone.utc),
    ]
    with pytest.raises(ValueError):
        coastwatch.composite_series(["chl_viirs_snpp"], BBOX, ends, days=2)


def test_mask_coast_on_a_series():
    v = np.ones((2, 5, 5), dtype=np.float32)
    v[:, :, 0] = np.nan  # land on the west edge
    ds = xr.Dataset({"value": (("time", "lat", "lon"), v)})
    m = coastwatch.mask_coast(ds, pixels=1)
    assert np.isnan(m["value"].values[:, :, 1]).all()  # the band next to land
    assert np.isfinite(m["value"].values[:, 2:4, 2:4]).all()


def test_fetch_range_skips_a_failing_chunk(erddap, monkeypatch):
    erddap({"noaacwNPPVIIRSchlaSectorVYDaily": _three_days()})
    real = coastwatch._fetch_window
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ERDDAP: giving up after 3 attempts")
        return real(*args, **kwargs)

    monkeypatch.setattr(coastwatch, "_fetch_window", flaky)
    ds = coastwatch.fetch_range(
        "chl_viirs_snpp",
        BBOX,
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 3, 23, tzinfo=timezone.utc),
        chunk_days=1,
    )
    assert (
        calls["n"] == 3
    )  # the window is clamped to the archive (Oct 1 to 3): three 1-day chunks, all tried
    assert (
        ds.sizes["time"] == 3
    )  # the later chunks still delivered (the fake serves every pass to each)
