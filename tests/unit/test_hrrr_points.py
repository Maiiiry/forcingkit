"""HRRR at points: .idx patterns, leads per cycle, water-cell weights, wind rotation and the bitemporal rows. No
network."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from forcingkit.fetchers import hrrr_points as hp

LOV = 262.5


def test_idx_patterns_name_the_lead():
    assert hp.idx_pattern("u10", 0) == ":UGRD:10 m above ground:anl:"
    assert hp.idx_pattern("u10", 6) == ":UGRD:10 m above ground:6 hour fcst:"
    assert hp.idx_pattern("vis", 18) == ":VIS:surface:18 hour fcst:"


def test_leads_per_cycle():
    assert hp.max_lead(pd.Timestamp("2026-10-14T06:00")) == 48
    assert hp.max_lead(pd.Timestamp("2026-10-14T07:00")) == 18
    assert hp.max_lead(pd.Timestamp("2019-06-01T12:00")) == 36


def test_key_for_a_cycle_and_lead():
    assert hp.s3_key(pd.Timestamp("2026-10-14T07:00"), 3).endswith(
        "hrrr.20261014/conus/hrrr.t07z.wrfsfcf03.grib2"
    )


def _grid():
    j, i = np.meshgrid(np.arange(30), np.arange(30), indexing="ij")
    lon = 286.0 + 0.03 * i  # 0..360 longitudes, as HRRR's
    lat = 40.5 + 0.03 * j
    land = (i < 10).astype(float)  # the western third is land
    return lon, lat, land


def test_weights_take_water_cells_only():
    lon, lat, land = _grid()
    # A point on the coast: its nearest cells are land, so the weights must reach east to water.
    p = {"coast": (-74.0 + 0.03 * 9, 40.5 + 0.03 * 15)}
    w = hp.PointWeights(lon, lat, land, p)
    field = np.where(land > 0.5, -999.0, 7.0)
    assert w(field)["coast"] == pytest.approx(7.0)


def test_weights_are_exact_on_a_cell_and_linear_fields_stay_close():
    lon, lat, land = _grid()
    on = {"on": (-74.0 + 0.03 * 20, 40.5 + 0.03 * 12)}
    w = hp.PointWeights(lon, lat, land, on)
    field = 3.0 * (lon - 360.0) + 2.0 * lat
    assert w(field)["on"] == pytest.approx(3.0 * on["on"][0] + 2.0 * on["on"][1])


def test_no_water_within_reach_gives_nan():
    lon, lat, _ = _grid()
    w = hp.PointWeights(lon, lat, np.ones_like(lon), {"inland": (-73.7, 40.9)})
    assert np.isnan(w(np.zeros_like(lon))["inland"])


def _fake_native(cycle, lead, fields, workdir, grid_relative=0):
    lon, lat, land = _grid()
    out = {}
    for n, name in enumerate(fields):
        out[name] = xr.DataArray(
            np.full(lon.shape, float(n + lead)),
            dims=("y", "x"),
            coords={"longitude": (("y", "x"), lon), "latitude": (("y", "x"), lat)},
            attrs={
                "GRIB_Latin1InDegrees": 38.5,
                "GRIB_LoVInDegrees": LOV,
                "GRIB_uvRelativeToGrid": grid_relative,
            },
        )
    out["land"] = xr.DataArray(land, dims=("y", "x"))
    return out


def test_rows_are_bitemporal_and_skip_leads_a_cycle_lacks(monkeypatch):
    monkeypatch.setattr(hp, "_fetch_lead", _fake_native)
    hp._weights_cache.clear()
    df = hp.fetch_points(
        ["2026-10-14T07:00"],
        [1, 18, 24],
        {"buoy": (-73.4, 40.8)},
        fields=("t2m", "vis"),
    )
    # f24 is past a 07Z cycle's 18 h: skipped.
    assert sorted(df["lead_h"].unique()) == [1, 18]
    row = df[(df.lead_h == 18) & (df.field == "vis")].iloc[0]
    assert row.issue_time == pd.Timestamp("2026-10-14T07:00")
    assert row.valid_time == pd.Timestamp("2026-10-15T01:00")
    assert row.value == pytest.approx(1 + 18)  # second field, offset by the lead


def test_an_absent_file_is_skipped(monkeypatch):
    monkeypatch.setattr(
        hp,
        "_fetch_lead",
        lambda c, lead, f, w: None if lead == 2 else _fake_native(c, lead, f, w),
    )
    hp._weights_cache.clear()
    df = hp.fetch_points(
        ["2026-10-14T07:00"], [1, 2], {"buoy": (-73.4, 40.8)}, fields=("t2m",)
    )
    assert list(df["lead_h"].unique()) == [1]


def test_grid_relative_winds_are_rotated(monkeypatch):
    # A grid-relative wind along +x at 73.4 W is rotated by cone * (lon - lov) to east and north.
    monkeypatch.setattr(
        hp,
        "_fetch_lead",
        lambda c, lead, f, w: _fake_native(c, lead, f, w, grid_relative=1),
    )
    hp._weights_cache.clear()
    df = hp.fetch_points(
        ["2026-10-14T07:00"], [1], {"buoy": (-73.4, 40.8)}, fields=("u10", "v10")
    )
    u = df[df.field == "u10"].value.iloc[0]
    v = df[df.field == "v10"].value.iloc[0]
    # The fake gives grid u = 0 + 1 and grid v = 1 + 1 (field order plus lead).
    alpha = np.deg2rad(np.sin(np.deg2rad(38.5)) * (-73.4 - (LOV - 360.0)))
    assert u == pytest.approx(np.cos(alpha) * 1.0 + np.sin(alpha) * 2.0)
    assert v == pytest.approx(-np.sin(alpha) * 1.0 + np.cos(alpha) * 2.0)


def test_unknown_fields_are_refused():
    with pytest.raises(ValueError):
        hp.fetch_points(
            ["2026-10-14T07:00"], [1], {"b": (-73.4, 40.8)}, fields=("cape",)
        )
