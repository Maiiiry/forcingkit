"""NECOFS full-depth subset (`read_volume`, `volume`) and the edge-neighbour table, on a synthetic
FVCOM-shaped file. No network.

Values encode where they came from (element or node index, layer, hour), so a test can check that
every number lands on its own element, node, layer and time."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from forcingkit.fetchers import necofs
from forcingkit.mesh_frames import element_neighbours

NX, NY, NSIG = 5, 4, 3  # nodes per row, rows, sigma layers


def grid_mesh():
    """A 5 x 4 node grid on lon 0..4, lat 0..3 with each square split along its SW-NE diagonal:
    returns (lon, lat, tri) with zero-based tri."""
    lon, lat = np.meshgrid(np.arange(NX, dtype=float), np.arange(NY, dtype=float))
    tri = []
    for j in range(NY - 1):
        for i in range(NX - 1):
            sw, se = j * NX + i, j * NX + i + 1
            nw, ne = sw + NX, se + NX
            tri += [[sw, se, ne], [sw, ne, nw]]
    return lon.ravel(), lat.ravel(), np.array(tri)


def element_value(elem, layer, hour):
    return elem + 100.0 * layer + 1000.0 * hour


def node_value(node, layer, hour):
    return node + 100.0 * layer + 1000.0 * hour


def fvcom_file(times: pd.DatetimeIndex) -> xr.Dataset:
    """An in-memory stand-in for a GOM7 history file holding `times`."""
    lon, lat, tri = grid_mesh()
    n_node, n_ele = lon.size, tri.shape[0]
    lonc, latc = lon[tri].mean(axis=1), lat[tri].mean(axis=1)
    hours = np.array(
        [(t - pd.Timestamp("2026-04-01")) / pd.Timedelta(hours=1) for t in times]
    )
    e, n, k = np.arange(n_ele), np.arange(n_node), np.arange(NSIG)
    ev = element_value(e[None, None], k[None, :, None], hours[:, None, None]).astype(
        np.float32
    )
    nvv = node_value(n[None, None], k[None, :, None], hours[:, None, None]).astype(
        np.float32
    )
    siglev = np.repeat(np.linspace(0, -1, NSIG + 1)[:, None], n_node, axis=1)
    siglay = 0.5 * (siglev[:-1] + siglev[1:])
    return xr.Dataset(
        {
            "nv": (("three", "nele"), (tri.T + 1).astype(np.int32)),
            "lon": (("node",), lon.astype(np.float32)),
            "lat": (("node",), lat.astype(np.float32)),
            "x": (("node",), (1000.0 * lon).astype(np.float32)),
            "y": (("node",), (1000.0 * lat).astype(np.float32)),
            "lonc": (("nele",), lonc.astype(np.float32)),
            "latc": (("nele",), latc.astype(np.float32)),
            "xc": (("nele",), (1000.0 * lonc).astype(np.float32)),
            "yc": (("nele",), (1000.0 * latc).astype(np.float32)),
            "h": (("node",), (10.0 + n).astype(np.float32)),
            "siglay": (("siglay", "node"), siglay.astype(np.float32)),
            "siglev": (("siglev", "node"), siglev.astype(np.float32)),
            "u": (("time", "siglay", "nele"), ev),
            "v": (("time", "siglay", "nele"), -ev),
            "ww": (("time", "siglay", "nele"), 1e-3 * ev),
            "temp": (("time", "siglay", "node"), nvv),
            "salinity": (("time", "siglay", "node"), -nvv),
            "zeta": (("time", "node"), nvv[:, 0] * 1e-3),
        },
        coords={"time": times},
    )


def day_file_times(stamp: str) -> pd.DatetimeIndex:
    """The hours a GOM7 daily history file stamped `stamp` holds: (D-1 01:00, D 00:00]."""
    d = pd.Timestamp(stamp)
    return pd.date_range(d - pd.Timedelta(hours=23), d, freq="1h")


# --- edge neighbours ---------------------------------------------------------------------------


def test_neighbours_of_a_split_square():
    tri = np.array([[0, 1, 2], [0, 2, 3]])
    nbe = element_neighbours(tri)
    # Edge 0-2 is opposite vertex 1 of element 0 and opposite vertex 2 of element 1.
    np.testing.assert_array_equal(nbe, [[-1, 1, -1], [-1, -1, 0]])


def test_neighbours_are_mutual_and_share_the_edge():
    _, _, tri = grid_mesh()
    nbe = element_neighbours(tri)
    for i, row in enumerate(nbe):
        for j, k in enumerate(row):
            if k < 0:
                continue
            assert i in nbe[k]
            edge = {tri[i, (j + 1) % 3], tri[i, (j + 2) % 3]}
            assert edge <= set(tri[k])


def test_boundary_edges_of_the_grid():
    _, _, tri = grid_mesh()
    nbe = element_neighbours(tri)
    # A (NX-1) x (NY-1) grid of squares has 2 * ((NX-1) + (NY-1)) boundary edges.
    assert (nbe < 0).sum() == 2 * ((NX - 1) + (NY - 1))


def test_an_edge_shared_by_three_elements_is_rejected():
    with pytest.raises(ValueError):
        element_neighbours(np.array([[0, 1, 2], [1, 0, 3], [0, 1, 4]]))


# --- one file ------------------------------------------------------------------------------------

INNER = (0.9, 0.9, 3.1, 2.1)  # centroids of the squares with lon 1..3, lat 1..2


def subset(times=None, wanted=None, bbox=INNER):
    times = day_file_times("2026-04-03") if times is None else times
    wanted = times if wanted is None else wanted
    return necofs._subset_volume(fvcom_file(times), times, bbox, wanted, "test://file")


def test_values_land_on_their_own_element_node_layer_and_hour():
    out = subset()
    hours = (
        pd.DatetimeIndex(out.time.values) - pd.Timestamp("2026-04-01")
    ) / pd.Timedelta(hours=1)
    e = out["element_id"].values
    n = out["node_id"].values
    k = np.arange(NSIG)
    expect_e = element_value(
        e[None, None], k[None, :, None], hours.values[:, None, None]
    )
    expect_n = node_value(n[None, None], k[None, :, None], hours.values[:, None, None])
    np.testing.assert_array_equal(out["u"].values, expect_e)
    np.testing.assert_array_equal(out["v"].values, -expect_e)
    np.testing.assert_allclose(out["ww"].values, 1e-3 * expect_e, rtol=1e-6)
    np.testing.assert_array_equal(out["temp"].values, expect_n)
    np.testing.assert_array_equal(out["salinity"].values, -expect_n)
    assert out["u"].dims == ("time", "siglay", "element")
    assert out["temp"].dims == ("time", "siglay", "node")
    assert out.sizes["siglev"] == NSIG + 1


def test_selection_is_by_element_centroid_and_brings_every_node():
    out = subset()
    lonc, latc = out["lonc"].values, out["latc"].values
    assert np.all((lonc >= INNER[0]) & (lonc <= INNER[2]))
    assert np.all((latc >= INNER[1]) & (latc <= INNER[3]))
    assert (
        out.sizes["element"] == 2 * 2 * 1
    )  # two squares across, one up, two triangles each
    # Triangles index the returned nodes, and agree with the full mesh.
    _, _, tri = grid_mesh()
    tri_back = out["node_id"].values[out["triangles"].values]
    np.testing.assert_array_equal(tri_back, tri[out["element_id"].values])


def test_static_fields_and_coordinates_follow_the_nodes():
    out = subset()
    n = out["node_id"].values
    np.testing.assert_array_equal(out["h"].values, 10.0 + n)
    np.testing.assert_allclose(out["x"].values, 1000.0 * out["node_lon"].values)
    np.testing.assert_allclose(out["xc"].values, 1000.0 * out["lonc"].values, rtol=1e-6)
    assert out["siglay"].dims == ("siglay", "node")
    assert out["siglay"].values[0, 0] > out["siglay"].values[-1, 0]  # surface first


def test_neighbours_outside_the_subset_are_not_mistaken_for_the_coast():
    _, _, tri = grid_mesh()
    full = element_neighbours(tri)
    out = subset()
    ids = out["element_id"].values
    nb = out["neighbours"].values
    for i, e in enumerate(ids):
        for j in range(3):
            g = full[e, j]
            if g < 0:
                assert nb[i, j] == necofs.NO_NEIGHBOUR
            elif g in ids:
                assert ids[nb[i, j]] == g
            else:
                assert nb[i, j] == necofs.OUTSIDE_SUBSET
    # The inner box touches no mesh boundary, so no edge is coast; some neighbours lie outside.
    assert not (nb == necofs.NO_NEIGHBOUR).any()
    assert (nb == necofs.OUTSIDE_SUBSET).any()


def test_boundary_nodes_come_from_the_whole_mesh():
    out = subset(bbox=(-1, -1, 5, 4))  # everything
    lon, lat = out["node_lon"].values, out["node_lat"].values
    perimeter = (lon == 0) | (lon == NX - 1) | (lat == 0) | (lat == NY - 1)
    np.testing.assert_array_equal(out["on_boundary"].values, perimeter)
    assert (out["neighbours"].values == necofs.NO_NEIGHBOUR).sum() == 2 * (
        NX - 1 + NY - 1
    )
    inner = subset()
    assert not inner["on_boundary"].values.any()


def test_hours_the_file_lacks_are_left_out_and_none_raises():
    times = day_file_times("2026-04-03")
    wanted = pd.date_range("2026-04-02T22", periods=4, freq="1h")  # 22, 23, 00, 01
    out = subset(times, wanted)
    np.testing.assert_array_equal(out.time.values, wanted[:3].values)
    with pytest.raises(RuntimeError):
        subset(times, pd.date_range("2026-04-05", periods=2, freq="1h"))


def test_an_empty_box_raises():
    with pytest.raises(ValueError):
        subset(bbox=(10.0, 10.0, 11.0, 11.0))


# --- a window across files, and the cache ------------------------------------------------------


@pytest.fixture
def archive(monkeypatch, tmp_path):
    """Daily history files served from memory; records which files were read."""
    monkeypatch.setenv("FORCINGKIT_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        necofs,
        "get_necofs_url",
        lambda t: f"file://{(t.normalize() + pd.Timedelta(days=1)):%Y_%m_%d}",
    )
    reads: list[str] = []
    held = {"2026_04_02", "2026_04_03", "2026_04_04"}

    def reader(bbox, start, hours, dap_url=None):
        stamp = dap_url.removeprefix("file://")
        reads.append(stamp)
        times = (
            day_file_times(stamp.replace("_", "-"))
            if stamp in held
            else pd.DatetimeIndex([])
        )
        if not len(times):
            raise RuntimeError(f"{dap_url} not found")
        wanted = pd.date_range(pd.Timestamp(start), periods=hours, freq="1h")
        return necofs._subset_volume(fvcom_file(times), times, bbox, wanted, dap_url)

    return reader, reads


def test_a_window_is_assembled_across_archive_files(archive):
    reader, reads = archive
    out = necofs.volume(
        INNER, "2026-04-02T00", "2026-04-03T12", cache=False, reader=reader
    )
    expect = pd.date_range("2026-04-02T00", "2026-04-03T12", freq="1h")
    np.testing.assert_array_equal(out.time.values, expect.values)
    assert reads == [
        "2026_04_02",
        "2026_04_03",
        "2026_04_04",
    ]  # midnight lives in its own day
    hours = (expect - pd.Timestamp("2026-04-01")) / pd.Timedelta(hours=1)
    e0 = out["element_id"].values[0]
    np.testing.assert_array_equal(
        out["u"].values[:, 1, 0], element_value(e0, 1, hours.values)
    )
    assert out["h"].dims == ("node",)  # static fields are not stacked in time
    assert out.attrs["urls"] == [
        "file://2026_04_02",
        "file://2026_04_03",
        "file://2026_04_04",
    ]


def test_timezone_aware_bounds_are_taken_as_utc(archive):
    reader, _ = archive
    out = necofs.volume(
        INNER, "2026-04-01T20:00-04:00", "2026-04-02T02:00Z", cache=False, reader=reader
    )
    assert pd.Timestamp(out.time.values[0]) == pd.Timestamp("2026-04-02T00")
    assert len(out.time) == 3


def test_a_missing_hour_raises_instead_of_returning_a_short_window(archive):
    reader, _ = archive
    with pytest.raises(RuntimeError):
        necofs.volume(
            INNER, "2026-04-04T12", "2026-04-05T02", cache=False, reader=reader
        )


def test_the_cache_returns_the_same_volume_without_reading_again(archive):
    reader, reads = archive
    first = necofs.volume(INNER, "2026-04-02T05", "2026-04-02T08", reader=reader)
    n_reads = len(reads)
    second = necofs.volume(INNER, "2026-04-02T05", "2026-04-02T08", reader=reader)
    assert len(reads) == n_reads
    xr.testing.assert_identical(first.drop_attrs(), second.drop_attrs())
    assert second.attrs["schema"] == necofs.VOLUME_SCHEMA


def test_the_cache_key_depends_on_box_window_and_schema():
    t0, t1 = pd.Timestamp("2026-04-02"), pd.Timestamp("2026-04-03")
    key = necofs.volume_cache_key(INNER, t0, t1)
    assert key == necofs.volume_cache_key(list(INNER), t0, t1)
    assert key != necofs.volume_cache_key((0.9, 0.9, 3.1, 2.2), t0, t1)
    assert key != necofs.volume_cache_key(INNER, t0, t1 + pd.Timedelta(hours=1))
