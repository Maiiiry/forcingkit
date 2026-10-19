"""Mesh frames contract, adapter registry and flat-shaded renderer, on synthetic meshes (no network)."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from forcingkit import mesh_frames as mf
from forcingkit import render

# A 10 x 10 degree box drawn at 10 pixels per degree; pixel (i, j) has its centre at
# lon = i/10 + 0.05, lat = 10 - j/10 - 0.05.
BBOX = (0.0, 0.0, 10.0, 10.0)
SIZE = (100, 100)
RED = (255, 0, 0, 255)
BLUE = (0, 0, 255, 255)


def square(n_t: int = 1) -> mf.MeshFrames:
    """The square [2, 8] x [2, 8] split along its SW-NE diagonal: element 0 south-east of it,
    element 1 north-west."""
    lon = np.array([2.0, 8.0, 8.0, 2.0])
    lat = np.array([2.0, 2.0, 8.0, 8.0])
    tri = np.array([[0, 1, 2], [0, 2, 3]])
    times = pd.date_range("2026-01-01", periods=n_t, freq="1h")
    u = np.tile(np.array([[1.0, 0.0]], dtype=np.float32), (n_t, 1))
    v = np.zeros_like(u)
    return mf.MeshFrames(times, lon, lat, tri, u, v)


def two_colour(speed):
    """Element with speed > 0.5 red, otherwise blue."""
    out = np.empty((len(speed), 4), dtype=np.uint8)
    out[:] = BLUE
    out[np.asarray(speed) > 0.5] = RED
    return out


@pytest.fixture(autouse=True)
def _fresh_cache():
    render.clear_cache()
    yield
    render.clear_cache()


def frame(frames=None, **kw) -> np.ndarray:
    frames = frames or square()
    return np.asarray(next(render.render_frames(frames, BBOX, SIZE, two_colour, **kw)))


# Contract --------------------------------------------------------------------------------------


def test_contract_normalises_times_to_utc_and_checks_shapes():
    f = square(3)
    assert str(f.times.tz) == "UTC" and len(f) == 3
    assert f.tri.dtype == np.int64 and f.u.shape == (3, 2)
    with pytest.raises(ValueError):
        mf.MeshFrames(f.times, f.node_lon, f.node_lat, f.tri, f.u[:, :1], f.v)
    with pytest.raises(ValueError):
        mf.MeshFrames(f.times, f.node_lon, f.node_lat, f.tri + 3, f.u, f.v)


# Projection ------------------------------------------------------------------------------------


def test_projection_maps_bbox_corners_to_image_corners():
    x, y = render.lonlat_to_px(
        np.array([-73.0, -72.6, -72.8]),
        np.array([41.15, 40.95, 41.05]),
        (-73.0, 40.95, -72.6, 41.15),
        (1600, 900),
    )
    np.testing.assert_allclose(x, [0.0, 1600.0, 800.0], atol=1e-9)
    np.testing.assert_allclose(y, [0.0, 900.0, 450.0], atol=1e-9)


def test_pixels_align_with_the_bbox_grid():
    idx = render.element_index_image(square(), BBOX, SIZE)
    assert idx.shape == (100, 100) and idx.dtype == np.int32
    # The square spans lon 2..8 -> columns 20..79 and lat 8..2 -> rows 20..79.
    row = 50
    assert idx[row, 19] == -1 and idx[row, 20] == 1
    assert idx[row, 79] == 0 and idx[row, 80] == -1
    col = 50
    assert idx[19, col] == -1 and idx[20, col] == 1
    assert idx[79, col] == 0 and idx[80, col] == -1


def test_index_image_matches_point_in_triangle_at_pixel_centres():
    """On an irregular mesh and a non-square box, every pixel centre maps to the element that
    contains it, as an independent point location finds it."""
    from scipy.spatial import Delaunay

    rng = np.random.default_rng(0)
    bbox, size = (-1.0, 50.0, 2.0, 51.0), (90, 40)
    pts = np.column_stack([rng.uniform(-0.8, 1.7, 300), rng.uniform(50.1, 50.9, 300)])
    d = Delaunay(pts)
    f = mf.MeshFrames(
        pd.DatetimeIndex(["2026-01-01"]),
        pts[:, 0],
        pts[:, 1],
        d.simplices,
        np.zeros((1, len(d.simplices))),
        np.zeros((1, len(d.simplices))),
    )
    idx = render.element_index_image(f, bbox, size)
    cols, rows = np.meshgrid(np.arange(size[0]), np.arange(size[1]))
    lon = bbox[0] + (cols + 0.5) * (bbox[2] - bbox[0]) / size[0]
    lat = bbox[3] - (rows + 0.5) * (bbox[3] - bbox[1]) / size[1]
    expected = d.find_simplex(np.column_stack([lon.ravel(), lat.ravel()])).reshape(
        idx.shape
    )
    assert (expected >= 0).sum() > 1000
    np.testing.assert_array_equal(idx, expected)


# Flat shading and transparency -----------------------------------------------------------------


def test_each_element_is_one_flat_colour():
    img = frame()
    assert img.shape == (100, 100, 4)
    # South-east element (speed 1) red, north-west element (speed 0) blue, exactly.
    assert tuple(img[70, 70]) == RED  # lon 7.05, lat 2.95
    assert tuple(img[30, 30]) == BLUE  # lon 3.05, lat 6.95
    idx = render.element_index_image(square(), BBOX, SIZE)
    assert np.all(img[idx == 0] == RED) and np.all(img[idx == 1] == BLUE)


def test_outside_the_mesh_is_transparent_or_background():
    img = frame()
    assert tuple(img[5, 5]) == (0, 0, 0, 0) and tuple(img[95, 95]) == (0, 0, 0, 0)
    grey = (200, 200, 200, 255)
    img = frame(background=grey)
    assert tuple(img[5, 5]) == grey and tuple(img[70, 70]) == RED


def test_nan_elements_render_as_outside():
    f = square()
    f.u[0, 0] = np.nan
    img = frame(f)
    assert tuple(img[70, 70]) == (0, 0, 0, 0)


def test_rgb_colour_functions_get_full_alpha():
    img = np.asarray(
        next(render.render_frames(square(), BBOX, SIZE, lambda s: two_colour(s)[:, :3]))
    )
    assert tuple(img[70, 70]) == RED


def test_edges_overlay_and_frames_vary_in_time():
    f = square(2)
    f.u[1] = [0.0, 1.0]
    imgs = [
        np.asarray(i)
        for i in render.render_frames(
            f, BBOX, SIZE, two_colour, edges=((0, 255, 0, 255), 1)
        )
    ]
    green = (0, 255, 0, 255)
    # The south edge (lat 2) falls between rows 79 and 80.
    assert any(tuple(imgs[0][r, 50]) == green for r in (79, 80))
    assert tuple(imgs[0][70, 70]) == RED and tuple(imgs[1][70, 70]) == BLUE
    assert tuple(imgs[1][30, 30]) == RED


def test_antialias_keeps_size_and_interior_colour():
    img = frame(antialias=3)
    assert img.shape == (100, 100, 4) and tuple(img[70, 70]) == RED


def test_index_image_is_cached_per_mesh_bbox_and_size():
    a = render.element_index_image(square(), BBOX, SIZE)
    assert render.element_index_image(square(), BBOX, SIZE) is a
    assert render.element_index_image(square(), BBOX, (50, 50)) is not a


def test_write_frames(tmp_path):
    f = square(3)
    paths = render.write_frames(
        render.render_frames(f, BBOX, SIZE, two_colour), tmp_path, times=f.times
    )
    assert [p.name for p in paths] == [
        "frame_0000.png",
        "frame_0001.png",
        "frame_0002.png",
    ]
    assert (tmp_path / "frames.json").exists()


# Colour functions ------------------------------------------------------------------------------


def test_lut_linear_interpolation_and_clamping():
    color = render.lut_color([(0.0, 0, 0, 0), (1.0, 200, 100, 50)])
    out = color(np.array([-1.0, 0.0, 0.5, 1.0, 5.0, np.nan]))
    assert out.shape == (6, 4) and out.dtype == np.uint8
    assert tuple(out[0]) == (0, 0, 0, 255)  # below first knot: first colour
    assert tuple(out[2]) == (100, 50, 25, 255)
    assert tuple(out[4]) == (200, 100, 50, 255)  # above last knot: last colour
    assert tuple(out[5]) == (0, 0, 0, 0)  # NaN: transparent


def test_lut_log_interpolation_and_floor():
    rows = [(0.1, 0, 0, 0), (1.0, 100, 100, 100)]
    color = render.lut_color(rows, log=True)
    out = color(np.array([0.0, 0.05, np.sqrt(0.1), 1.0, 3.0]))
    assert out[:2, 0].tolist() == [0, 0]  # zero and below the first knot
    assert out[2, 0] == 50  # geometric midpoint of the knots
    assert out[3, 0] == 100 and out[4, 0] == 100
    floored = render.lut_color(rows, log=True, floor=0.5)
    out = floored(np.array([0.3, 0.49, 0.5, 0.6]))
    assert out[:2, 0].tolist() == [0, 0]  # below the floor: first colour
    assert out[2, 0] == round(100 * np.log(5) / np.log(10))
    with pytest.raises(ValueError):
        render.lut_color([(0.0, 0, 0, 0), (1.0, 1, 1, 1)], log=True)


def test_lut_csv_and_named_ramps(tmp_path):
    p = tmp_path / "lut.csv"
    p.write_text("value,r,g,b\n# comment\n0.02,10,20,30\n1.0,210,220,230,128\n")
    rows = render.load_lut_csv(p)
    assert rows == [[0.02, 10, 20, 30], [1.0, 210, 220, 230, 128]]
    out = render.lut_color(rows)(np.array([0.0, 2.0]))
    assert tuple(out[0]) == (10, 20, 30, 255) and tuple(out[1]) == (210, 220, 230, 128)
    out = render.named_color("greys", 0.0, 2.0)(np.array([0.0, 2.0]))
    assert tuple(out[0]) == (240, 240, 240, 255) and tuple(out[1]) == (30, 30, 30, 255)
    assert render.default_color(np.array([0.3])).shape == (1, 4)


# Registry --------------------------------------------------------------------------------------


def test_registry_builds_the_time_axis_and_dispatches(monkeypatch):
    seen = {}

    def fake(bbox, times):
        seen["bbox"], seen["times"] = bbox, times
        f = square(len(times))
        f.times = times
        return f

    monkeypatch.setitem(mf._ADAPTERS, "fake", fake)
    assert {"necofs", "fake"} <= set(mf.available_models())
    out = mf.mesh_frames(
        "FAKE", BBOX, "2026-01-01T00:30", "2026-01-01T06:00", stride_h=2
    )
    assert seen["bbox"] == BBOX
    assert [t.hour for t in seen["times"]] == [0, 2, 4, 6]
    assert str(out.times.tz) == "UTC"
    with pytest.raises(KeyError):
        mf.mesh_frames("nosuchmodel", BBOX, "2026-01-01", "2026-01-01")
    with pytest.raises(ValueError):
        mf.mesh_frames("fake", (1, 1, 0, 0), "2026-01-01", "2026-01-01")


def test_necofs_adapter_spans_archive_days(monkeypatch):
    """Hours on either side of midnight come from two daily files and are concatenated in order."""
    from forcingkit.fetchers import necofs

    calls = []

    def fake_url(t):
        return f"file-{(t + pd.Timedelta(days=1)).date()}"

    def fake_read(bbox, start, hours, dap_url=None):
        calls.append((dap_url, pd.Timestamp(start), hours))
        times = pd.date_range(start, periods=hours, freq="1h")
        base = square(hours)
        return xr.Dataset(
            {
                "u": (
                    ("time", "element"),
                    np.full((hours, 2), times.hour[0], np.float32),
                ),
                "v": (("time", "element"), np.zeros((hours, 2), np.float32)),
                "triangles": (("element", "vertex"), base.tri.astype(np.int32)),
                "node_lon": (("node",), base.node_lon),
                "node_lat": (("node",), base.node_lat),
                "element_id": (("element",), np.array([10, 11])),
            },
            coords={"time": times},
        )

    monkeypatch.setattr(necofs, "get_necofs_url", fake_url)
    monkeypatch.setattr(necofs, "read_surface", fake_read)
    out = mf.mesh_frames("necofs", BBOX, "2026-03-01T22:00Z", "2026-03-02T02:00Z")
    assert [c[0] for c in calls] == ["file-2026-03-02", "file-2026-03-03"]
    # File D holds (D-1 01:00, D 00:00]: midnight belongs with the previous evening.
    assert [c[2] for c in calls] == [3, 2]
    assert [t.hour for t in out.times] == [22, 23, 0, 1, 2]
    assert out.u[:, 0].tolist() == [22, 22, 22, 1, 1]


# Packaged currents ramp ------------------------------------------------------------------------


def _lightness(rgb: np.ndarray) -> np.ndarray:
    """CIELAB L* (D65) of sRGB rows."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    y = lin @ np.array([0.2126729, 0.7151522, 0.0721750])
    f = np.where(y > (6 / 29) ** 3, np.cbrt(y), y / (3 * (6 / 29) ** 2) + 4 / 29)
    return 116 * f - 16


def test_currents_ramp_rows_and_lightness():
    rows = np.array(render.packaged_ramp_rows("currents_lis"))
    assert rows.shape[1] == 4 and 48 <= len(rows) <= 80
    speed, rgb = rows[:, 0], rows[:, 1:]
    assert speed[0] == pytest.approx(0.02) and speed[-1] == pytest.approx(3.1)
    assert np.all(np.diff(speed) > 0)
    for knot in (0.494, 2.418, 3.097):
        assert np.isclose(speed, knot, atol=5e-5).any()
    L = _lightness(rgb)
    assert np.all(np.diff(L) <= 0), "L* must not increase along the ramp"
    assert 92 <= L[0] < 95.7  # lighter than the yellow, darker than land at L* 95.7
    assert L[-1] == pytest.approx(25, abs=1.5)


def test_dark_currents_ramp_matches_axis_and_rises_in_lightness():
    light = np.array(render.packaged_ramp_rows("currents_lis"))
    dark = np.array(render.packaged_ramp_rows("currents_lis_dark"))
    assert len(dark) == 60
    np.testing.assert_array_equal(dark[:, 0], light[:, 0])  # swap by theme alone
    L = _lightness(dark[:, 1:])
    assert np.all(np.diff(L) >= 0), "L* must not decrease along the dark ramp"
    # Slack well above the dark basemap's water (rgb 27,27,29; L* 9.8); fast near white.
    assert L[0] >= 18 and L[-1] >= 85
    # Most of the change between 0.494 and 2.418 m/s.
    s = dark[:, 0]
    mid = L[np.isclose(s, 2.418, atol=5e-5)][0] - L[np.isclose(s, 0.494, atol=5e-5)][0]
    assert mid > 0.5 * (L[-1] - L[0])


@pytest.mark.parametrize("name", ["currents_lis", "currents_lis_dark"])
def test_currents_ramp_floor_log_axis_and_clamp(name):
    rows = render.packaged_ramp_rows(name)
    color = render.packaged_color(name)
    first, last = (
        tuple(int(x) for x in rows[0][1:]),
        tuple(int(x) for x in rows[-1][1:]),
    )
    out = color(np.array([0.0, 0.01, 0.02, 3.1, 5.0]))
    assert all(tuple(o[:3]) == first for o in out[:3])
    assert all(tuple(o[:3]) == last for o in out[3:])
    # Exactly on a row, the row's colour.
    mid = rows[len(rows) // 2]
    assert tuple(color(np.array([mid[0]]))[0, :3]) == tuple(int(x) for x in mid[1:])
