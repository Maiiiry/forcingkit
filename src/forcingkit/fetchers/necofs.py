import warnings
import logging
import pandas as pd
import numpy as np
import xarray as xr
from typing import Optional
from scipy.spatial import Delaunay
from forcingkit import settings


logger = logging.getLogger(__name__)


def get_metadata() -> dict:
    return {
        "id": "necofs",
        "name": "NECOFS FVCOM GOM7",
        "resolution_approx_m": 200.0,
        "type_desc": "Unstructured Triangular Mesh",
        "domain_bbox": [-77.0, 35.0, -65.0, 46.0],
    }


def supports_bbox(bbox: list[float]) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    # Rough check for GOM3 bounds (NECOFS FVCOM)
    if max_lat < 35.0 or min_lat > 46.0 or max_lon < -77.0 or min_lon > -65.0:
        return False
    return True


NECOFS_GOM7_URL = "http://www.smast.umassd.edu:8080/thredds/dodsC/models/fvcom/NECOFS/Forecasts/NECOFS_GOM7_FORECAST.nc"


def get_necofs_url(target_dt: pd.Timestamp) -> str:
    """
    Determine best NECOFS GOM7 URL by falling back to daily history archives.
    SMAST daily archives (e.g. 2026_03_03.nc) contain [Mar 2 01:00 to Mar 3 00:00].
    """
    import requests

    # NECOFS GOM7 daily history files contain data for the PREVIOUS day up to 00:00 of the CURRENT day.
    # Therefore to get data forward-looking from target_dt, we must ALWAYS fetch the file for the NEXT day.
    file_dt = target_dt.normalize() + pd.Timedelta(days=1)

    date_str = file_dt.strftime("%Y_%m_%d")
    history_url = f"http://www.smast.umassd.edu:8080/thredds/dodsC/models/fvcom/NECOFS/Archive/necofs_history/NECOFS_GOM7_{date_str}.nc"

    try:
        resp = requests.get(history_url + ".dds", timeout=5)
        if resp.status_code == 200:
            logger.info(f"Using historic NECOFS GOM7 archive: {history_url}")
            return history_url
    except requests.RequestException:
        pass

    logger.info("Falling back to NECOFS GOM7 rolling forecast.")
    return NECOFS_GOM7_URL


# Parent-ocean delivery schema. Bump when the layout or meaning of the boundary store changes, so
# a cached store built under an older schema can never be served for a newer request.
OBC_SCHEMA = "z-v3"


def necofs_archive_file_date(t: pd.Timestamp) -> pd.Timestamp:
    """Date stamp of the GOM7 daily history file that holds the record at time `t`.

    File D holds (D-1 01:00, D 00:00], so the record at exactly midnight lives in the file stamped
    with that same day, and every other hour in the file stamped the following day.
    """
    return (t - pd.Timedelta(hours=1)).normalize() + pd.Timedelta(days=1)


def z_levels_for_depth(
    max_depth_m: float, spacing_m: float
) -> tuple[np.ndarray, np.ndarray]:
    """Fixed z levels covering 0 to -max_depth_m: (centres, faces), both ordered bottom to top.

    The bottom face sits at or below -max_depth_m, so every wet donor column is spanned.
    """
    if spacing_m <= 0:
        raise ValueError(f"vertical spacing must be positive, got {spacing_m}")
    nz = max(1, int(np.ceil(max_depth_m / spacing_m)))
    faces = -spacing_m * np.arange(nz, -1, -1, dtype=np.float64)
    centres = 0.5 * (faces[:-1] + faces[1:])
    return centres, faces


def sigma_to_z(
    values: np.ndarray,
    z_layers: np.ndarray,
    z_bottom: np.ndarray,
    z_targets: np.ndarray,
) -> np.ndarray:
    """Interpolate sigma-layer columns onto fixed z levels.

    values, z_layers: (nsig, ny, nx), layer index 0 at the surface (FVCOM convention), z negative.
    z_bottom: (ny, nx), the sea floor (-h); NaN marks land.
    z_targets: (nz,) target level centres, any order.

    Returns (nz, ny, nx). Between the surface and the shallowest layer centre the shallowest value
    is used, and between the deepest layer centre and the sea floor the deepest value; levels below
    the sea floor, and land columns, are NaN.
    """
    nsig = values.shape[0]
    out = np.full((len(z_targets),) + values.shape[1:], np.nan, dtype=np.float64)
    land = ~np.isfinite(z_bottom)
    for n, zt in enumerate(z_targets):
        # Number of layer centres at or above this level; layers are ordered surface first.
        above = np.sum(z_layers >= zt, axis=0)
        upper = np.clip(above - 1, 0, nsig - 1)
        lower = np.clip(above, 0, nsig - 1)
        zu = np.take_along_axis(z_layers, upper[None], axis=0)[0]
        zl = np.take_along_axis(z_layers, lower[None], axis=0)[0]
        vu = np.take_along_axis(values, upper[None], axis=0)[0]
        vl = np.take_along_axis(values, lower[None], axis=0)[0]
        span = zu - zl
        w = np.where(span > 0, (zu - zt) / np.where(span > 0, span, 1.0), 0.0)
        w = np.clip(w, 0.0, 1.0)
        level = (1.0 - w) * vu + w * vl
        level[zt < z_bottom] = np.nan
        level[land] = np.nan
        out[n] = level
    return out


class Barycentric:
    """Linear interpolation from scattered points onto fixed targets, with the simplex search and
    barycentric weights computed once.

    Equivalent to `LinearNDInterpolator(tri, values)(targets)`, which repeats the search for every
    call: per parent hour that is one search per layer per variable (181 for 45 layers of four
    fields plus sea surface height), each over every
    target point. Values may carry leading dimensions: (..., npoints) -> (..., *shape). Targets
    outside the triangulation are NaN.
    """

    def __init__(self, tri: Delaunay, targets: np.ndarray, shape: tuple[int, ...]):
        simplex = tri.find_simplex(targets)
        self.inside = simplex >= 0
        s = np.where(self.inside, simplex, 0)
        transform = tri.transform[s]
        b = np.einsum("ijk,ik->ij", transform[:, :2], targets - transform[:, 2])
        self.weights = np.column_stack([b, 1.0 - b.sum(axis=1)])
        self.vertices = tri.simplices[s]
        self.shape = shape

    def restrict(self, inside: np.ndarray) -> "Barycentric":
        """Also treat targets outside `inside` (flat, one per target) as outside."""
        self.inside = self.inside & inside
        return self

    def __call__(self, values: np.ndarray) -> np.ndarray:
        values = np.asarray(values, dtype=np.float64)
        out = np.einsum("...pk,pk->...p", values[..., self.vertices], self.weights)
        out[..., ~self.inside] = np.nan
        return out.reshape(values.shape[:-1] + self.shape)


class MeshBarycentric(Barycentric):
    """Linear interpolation on the triangles of an unstructured mesh, as FVCOM itself interpolates
    node values within an element.

    A Delaunay triangulation of the mesh nodes also covers land between them (a peninsula, an
    island, the coast between two estuaries) and fills it with values blended across it. Here a
    target takes values only from the mesh element that contains it, and a target inside no
    element (land, or outside the mesh) is NaN. `triangles` are zero-based node indices, one row
    per element.
    """

    def __init__(
        self,
        lon: np.ndarray,
        lat: np.ndarray,
        triangles: np.ndarray,
        targets: np.ndarray,
        shape: tuple[int, ...],
    ):
        from matplotlib.tri import Triangulation

        mesh = Triangulation(lon, lat, triangles)
        element = np.asarray(mesh.get_trifinder()(targets[:, 0], targets[:, 1]))
        self.inside = element >= 0
        self.vertices = triangles[np.where(self.inside, element, 0)]
        x, y = lon[self.vertices], lat[self.vertices]
        # Barycentric coordinates of each target in its element.
        det = (y[:, 1] - y[:, 2]) * (x[:, 0] - x[:, 2]) + (x[:, 2] - x[:, 1]) * (
            y[:, 0] - y[:, 2]
        )
        det = np.where(self.inside, det, 1.0)
        dx, dy = targets[:, 0] - x[:, 2], targets[:, 1] - y[:, 2]
        b0 = ((y[:, 1] - y[:, 2]) * dx + (x[:, 2] - x[:, 1]) * dy) / det
        b1 = ((y[:, 2] - y[:, 0]) * dx + (x[:, 0] - x[:, 2]) * dy) / det
        self.weights = np.column_stack([b0, b1, 1.0 - b0 - b1])
        self.shape = shape


PARENT_RECORD_DIMS = {
    "u": ("z", "lat", "lon"),
    "v": ("z", "lat", "lon"),
    "temp": ("z", "lat", "lon"),
    "salt": ("z", "lat", "lon"),
    "zeta": ("lat", "lon"),
}

PARENT_UNITS = {
    "u": ("m s-1", "eastward_sea_water_velocity"),
    "v": ("m s-1", "northward_sea_water_velocity"),
    "temp": ("degree_Celsius", "sea_water_temperature"),
    "salt": ("1e-3", "sea_water_practical_salinity"),
    "zeta": ("m", "sea_surface_height_above_geoid"),
    "h": ("m", "sea_floor_depth_below_geoid"),
}


def _parent_static(lon_axis, lat_axis, z_centres, z_faces, h_grid, attrs) -> xr.Dataset:
    mask = np.isfinite(h_grid).astype(np.int8)
    ds = xr.Dataset(
        data_vars={
            "h": (("lat", "lon"), h_grid.astype(np.float32)),
            "mask": (("lat", "lon"), mask),
            "z_face": (("z_face",), z_faces),
        },
        coords={"z": z_centres, "lat": lat_axis, "lon": lon_axis},
        attrs=attrs,
    )
    ds["h"].attrs.update(units="m", standard_name="sea_floor_depth_below_geoid")
    ds["mask"].attrs.update(
        long_name="1 where NECOFS has ocean, 0 on land or outside the mesh"
    )
    ds["z"].attrs.update(
        units="m", positive="up", axis="Z", long_name="level centre depth"
    )
    ds["z_face"].attrs.update(
        units="m", positive="up", long_name="level faces, bottom to top"
    )
    ds["lat"].attrs.update(units="degrees_north", standard_name="latitude", axis="Y")
    ds["lon"].attrs.update(units="degrees_east", standard_name="longitude", axis="X")
    return ds


def iter_parent(
    start_date: str,
    duration_hours: int,
    bbox: list[float],
    pad_cells: int = 3,
    vertical_spacing_m: float = 2.0,
):
    """NECOFS GOM7 as a parent ocean, one hourly record at a time.

    Yields `("static", Dataset)` once (lon, lat, z, z_face, h, mask and the store attributes),
    then `("record", time, {u, v, temp, salt, zeta})` for each of `duration_hours` hours starting
    at `start_date`, in order. u, v, temp and salt are (z, lat, lon) and zeta (lat, lon) on a
    regular 0.002 degree grid covering `bbox` plus `pad_cells` donor cells on every side, so the
    parent brackets the child. The vertical axis is true depth: each FVCOM sigma layer is placed at
    z = siglay * (h + zeta) + zeta for its column and interpolated onto fixed levels every
    `vertical_spacing_m` metres, ordered bottom to top. Land, points outside the mesh and levels
    below the sea floor are NaN. Land is what lies inside no element of the NECOFS mesh: node
    fields are interpolated within the containing element, and element fields (u, v) are NaN
    wherever the node mesh is.

    Raises rather than returning a short record: an unreachable archive file, a missing hour or a
    failed interpolation ends the delivery with an error.
    """
    import concurrent.futures

    min_lon, min_lat, max_lon, max_lat = bbox
    if max_lat < 35.0 or min_lat > 46.0 or max_lon < -77.0 or min_lon > -65.0:
        raise ValueError(f"Bounding box {bbox} outside NECOFS domain.")

    target_dt = pd.to_datetime(start_date)
    if target_dt.tzinfo is not None:
        target_dt = target_dt.tz_convert("UTC").tz_localize(None)

    d_spacing = 0.002
    pad = pad_cells * d_spacing
    lon_axis = np.arange(min_lon - pad, max_lon + pad + d_spacing / 2, d_spacing)
    lat_axis = np.arange(min_lat - pad, max_lat + pad + d_spacing / 2, d_spacing)
    lon_grid, lat_grid = np.meshgrid(lon_axis, lat_axis)
    target_pts = np.column_stack((lon_grid.ravel(), lat_grid.ravel()))
    shape = lon_grid.shape

    at_node = at_elem = None
    h_grid = siglay_grid = None
    z_centres = None
    max_workers = settings.max_workers()

    current_dt = target_dt
    end_dt = target_dt + pd.Timedelta(hours=duration_hours - 1)
    while current_dt <= end_dt:
        # get_necofs_url(t) opens the file stamped t.normalize() + 1 day.
        file_date = necofs_archive_file_date(current_dt)
        dap_url = get_necofs_url(file_date - pd.Timedelta(days=1))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ds_raw = xr.open_dataset(dap_url, engine="pydap", decode_times=False)
        drop_vars = [v for v in ["Itime", "Itime2"] if v in ds_raw.variables]
        ds = xr.decode_cf(ds_raw.drop_vars(drop_vars))
        ds_times = pd.DatetimeIndex(ds.time.values)
        if ds_times.tz is not None:
            ds_times = ds_times.tz_convert("UTC").tz_localize(None)

        if at_node is None:
            logger.info(
                "Building NECOFS parent interpolation weights and vertical grid..."
            )
            node_lon = np.asarray(ds["lon"].values, dtype=np.float64)
            node_lat = np.asarray(ds["lat"].values, dtype=np.float64)
            triangles = np.asarray(ds["nv"].values).T.astype(np.int64) - 1
            at_node = MeshBarycentric(node_lon, node_lat, triangles, target_pts, shape)
            at_elem = Barycentric(
                Delaunay(np.column_stack((ds["lonc"].values, ds["latc"].values))),
                target_pts,
                shape,
            ).restrict(at_node.inside)
            h_grid = at_node(ds["h"].values)
            siglay_grid = at_node(np.asarray(ds["siglay"].values))  # surface first
            if not np.any(np.isfinite(h_grid)):
                raise ValueError(f"No NECOFS ocean inside {bbox}")
            z_centres, z_faces = z_levels_for_depth(
                float(np.nanmax(h_grid)), vertical_spacing_m
            )
            yield (
                "static",
                _parent_static(
                    lon_axis,
                    lat_axis,
                    z_centres,
                    z_faces,
                    h_grid,
                    {
                        "type": "NECOFS/FVCOM GOM7 parent ocean",
                        "source": "UMass Dartmouth SMAST",
                        "schema": OBC_SCHEMA,
                        "pad_cells": pad_cells,
                        "vertical_spacing_m": vertical_spacing_m,
                        "requested_bbox": list(bbox),
                    },
                ),
            )

        wanted = pd.date_range(current_dt, min(end_dt, ds_times[-1]), freq="1h")
        idx = ds_times.get_indexer(wanted)
        if len(wanted) == 0 or (idx < 0).any():
            raise RuntimeError(
                f"{dap_url} lacks hourly records from {current_dt} "
                f"(holds {ds_times[0]} to {ds_times[-1]})"
            )
        logger.info(f"Extracting {len(wanted)} hours from {dap_url}...")

        def process(t_idx):
            snap = ds.isel(time=int(t_idx))
            zeta_t = at_node(snap["zeta"].values)
            z_bottom = np.where(np.isfinite(zeta_t), -h_grid, np.nan)
            z_layers = siglay_grid * (h_grid + zeta_t)[None] + zeta_t[None]
            fields = {
                "u": at_elem(snap["u"].values),
                "v": at_elem(snap["v"].values),
                "temp": at_node(snap["temp"].values),
                "salt": at_node(snap["salinity"].values),
            }
            out = {
                k: sigma_to_z(v, z_layers, z_bottom, z_centres).astype(np.float32)
                for k, v in fields.items()
            }
            out["zeta"] = zeta_t.astype(np.float32)
            return out

        # In batches of max_workers, so at most that many records are held at once.
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            for b in range(0, len(idx), max_workers):
                batch = idx[b : b + max_workers]
                for t, out in zip(
                    wanted[b : b + len(batch)], executor.map(process, batch)
                ):
                    yield ("record", t, out)

        current_dt = wanted[-1] + pd.Timedelta(hours=1)


def fetch_necofs_boundary_conditions(
    start_date: str,
    duration_hours: int,
    bbox: list[float],
    pad_cells: int = 3,
    vertical_spacing_m: float = 2.0,
) -> Optional[xr.Dataset]:
    """`iter_parent` assembled into one in-memory Dataset, for callers that want it whole. The
    service streams the records to disk instead (`dispatcher.dispatch_obc_request`)."""
    static = None
    times: list = []
    collected: dict[str, list[np.ndarray]] = {k: [] for k in PARENT_RECORD_DIMS}
    for item in iter_parent(
        start_date, duration_hours, bbox, pad_cells, vertical_spacing_m
    ):
        if item[0] == "static":
            static = item[1]
            continue
        _, t, rec = item
        times.append(t)
        for k in collected:
            collected[k].append(rec[k])
    if static is None:
        return None
    ds_out = static.assign(
        {
            k: (("time",) + dims, np.stack(collected[k]))
            for k, dims in PARENT_RECORD_DIMS.items()
        }
    ).assign_coords(time=times)
    for name, (unit, standard_name) in PARENT_UNITS.items():
        ds_out[name].attrs.update(units=unit, standard_name=standard_name)
    return ds_out


def _index_runs(idx: np.ndarray, max_gap: int = 2048) -> list[tuple[int, int]]:
    """Contiguous runs [start, stop] covering sorted `idx`, merging gaps up to `max_gap`.

    An OPeNDAP subset reads one hyperslab per run: a few slabs (a little extra data) are far faster than one
    request per element, and far smaller than the whole mesh.
    """
    runs: list[tuple[int, int]] = []
    for i in np.asarray(idx, dtype=np.int64):
        if runs and i - runs[-1][1] <= max_gap:
            runs[-1] = (runs[-1][0], int(i))
        else:
            runs.append((int(i), int(i)))
    return runs


def read_surface(
    bbox: list[float],
    start_time: str | pd.Timestamp,
    hours: int,
    dap_url: str | None = None,
) -> xr.Dataset:
    """Surface currents on the FVCOM mesh itself (no regridding, no vertical conversion), for the elements whose
    centres lie in `bbox` = [min_lon, min_lat, max_lon, max_lat], hourly from `start_time` for `hours`.

    Returns dims (time, element, node, vertex): `u`, `v` (time, element; m/s, top sigma layer), `lonc`, `latc`
    (element centres), `triangles` (element, vertex; indices into the returned nodes), `node_lon`, `node_lat`. For
    a map or a quick look at the flow; boundary forcing should use `iter_parent`. Reads the rolling GOM7 forecast
    unless `dap_url` names another file (for example an archive day from `get_necofs_url`).
    """
    url = dap_url or NECOFS_GOM7_URL
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ds_raw = xr.open_dataset(url, engine="pydap", decode_times=False)
    drop_vars = [v for v in ["Itime", "Itime2"] if v in ds_raw.variables]
    ds = xr.decode_cf(ds_raw.drop_vars(drop_vars))
    times = pd.DatetimeIndex(ds.time.values)
    if times.tz is not None:
        times = times.tz_convert("UTC").tz_localize(None)

    min_lon, min_lat, max_lon, max_lat = bbox
    lonc = np.asarray(ds["lonc"].values, dtype=np.float64)
    latc = np.asarray(ds["latc"].values, dtype=np.float64)
    elems = np.flatnonzero(
        (lonc >= min_lon) & (lonc <= max_lon) & (latc >= min_lat) & (latc <= max_lat)
    )
    if elems.size == 0:
        raise ValueError(f"No NECOFS elements inside {bbox}")
    nv = (
        np.asarray(ds["nv"].values).T.astype(np.int64) - 1
    )  # (element, 3), zero-based node indices
    tri_global = nv[elems]
    nodes, tri_local = np.unique(tri_global, return_inverse=True)
    tri_local = tri_local.reshape(tri_global.shape).astype(np.int32)

    start = pd.Timestamp(start_time)
    start = start.tz_convert("UTC").tz_localize(None) if start.tzinfo else start
    wanted = pd.date_range(start.floor("h"), periods=hours, freq="1h")
    t_idx = times.get_indexer(wanted)
    keep = t_idx >= 0
    if not keep.any():
        raise RuntimeError(
            f"{url} holds {times[0]} to {times[-1]}, none of {wanted[0]} to {wanted[-1]}"
        )
    t_idx, wanted = t_idx[keep], wanted[keep]
    t0, t1 = int(t_idx.min()), int(t_idx.max())

    u = np.empty((len(wanted), elems.size), dtype=np.float32)
    v = np.empty_like(u)
    col = 0
    for a, b in _index_runs(elems):
        sel = elems[(elems >= a) & (elems <= b)]
        slab = {"time": slice(t0, t1 + 1), "siglay": 0, "nele": slice(a, b + 1)}
        us = np.asarray(ds["u"].isel(slab).values)[t_idx - t0][:, sel - a]
        vs = np.asarray(ds["v"].isel(slab).values)[t_idx - t0][:, sel - a]
        u[:, col : col + sel.size] = us
        v[:, col : col + sel.size] = vs
        col += sel.size
    logger.info(
        f"NECOFS surface: {elems.size} elements, {len(wanted)} hours from {url}"
    )

    return xr.Dataset(
        {
            "u": (
                ("time", "element"),
                u,
                {"units": "m s-1", "long_name": "eastward current, top sigma layer"},
            ),
            "v": (
                ("time", "element"),
                v,
                {"units": "m s-1", "long_name": "northward current, top sigma layer"},
            ),
            "lonc": (("element",), lonc[elems].astype(np.float32)),
            "latc": (("element",), latc[elems].astype(np.float32)),
            "triangles": (("element", "vertex"), tri_local),
            "node_lon": (
                ("node",),
                np.asarray(ds["lon"].values)[nodes].astype(np.float32),
            ),
            "node_lat": (
                ("node",),
                np.asarray(ds["lat"].values)[nodes].astype(np.float32),
            ),
            "element_id": (("element",), elems.astype(np.int64)),
        },
        coords={"time": wanted},
        attrs={
            "type": "NECOFS/FVCOM GOM7 surface currents on the model mesh",
            "source": "UMass Dartmouth SMAST",
            "url": url,
            "requested_bbox": list(bbox),
        },
    )


# Full-depth subset schema. Bump when the layout or meaning of a cached volume changes.
VOLUME_SCHEMA = "volume-v1"

# Neighbour codes in the `neighbours` table of `read_volume` and `volume`.
NO_NEIGHBOUR = -1  # the edge is on the mesh boundary (coast or open boundary)
OUTSIDE_SUBSET = -2  # the neighbour exists but lies outside the subset


def _open_archive(url: str) -> tuple[xr.Dataset, pd.DatetimeIndex]:
    """An FVCOM file opened over OPeNDAP with CF times decoded, and its times as naive UTC."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ds_raw = xr.open_dataset(url, engine="pydap", decode_times=False)
    drop_vars = [v for v in ["Itime", "Itime2"] if v in ds_raw.variables]
    ds = xr.decode_cf(ds_raw.drop_vars(drop_vars))
    times = pd.DatetimeIndex(ds.time.values)
    if times.tz is not None:
        times = times.tz_convert("UTC").tz_localize(None)
    return ds, times


def _read_columns(
    da: xr.DataArray, dim: str, idx: np.ndarray, t_slice: slice | None
) -> np.ndarray:
    """`da` at the sorted indices `idx` of its last dimension `dim`, read in a few hyperslabs
    fetched concurrently (`settings.max_workers`).

    Node indices in a coastal box are scattered across the mesh, so runs merge only short gaps:
    for the Fishers Island box a 2048 gap reads one slab of 76,356 nodes to get 1,829, while 256
    reads 59 slabs of 1,877 in a fifth of the time. Each slab costs a server round trip of about
    0.3 to 1 s, hence the threads."""
    import concurrent.futures

    runs = _index_runs(idx, max_gap=256)

    def fetch(run):
        a, b = run
        sel = idx[(idx >= a) & (idx <= b)]
        slab = {dim: slice(a, b + 1)}
        if t_slice is not None:
            slab["time"] = t_slice
        return np.asarray(da.isel(slab).values)[..., sel - a]

    with concurrent.futures.ThreadPoolExecutor(settings.max_workers()) as pool:
        blocks = list(pool.map(fetch, runs))
    return np.concatenate(blocks, axis=-1).astype(np.float32, copy=False)


VOLUME_VARIABLE_ATTRS = {
    "u": {"units": "m s-1", "long_name": "eastward current at element layer centres"},
    "v": {"units": "m s-1", "long_name": "northward current at element layer centres"},
    "ww": {
        "units": "m s-1",
        "long_name": "upward velocity in z at element layer centres (FVCOM ww, not omega)",
    },
    "temp": {
        "units": "degree_Celsius",
        "long_name": "temperature at node layer centres",
    },
    "salinity": {"units": "1e-3", "long_name": "salinity at node layer centres"},
    "zeta": {"units": "m", "long_name": "surface elevation at nodes", "positive": "up"},
    "h": {
        "units": "m",
        "long_name": "depth below the geoid at nodes",
        "positive": "down",
    },
    "siglay": {"long_name": "sigma at layer centres, surface first", "positive": "up"},
    "siglev": {
        "long_name": "sigma at layer interfaces, surface first",
        "positive": "up",
    },
    "x": {"units": "m", "long_name": "node easting, model Cartesian (EPSG:26984)"},
    "y": {"units": "m", "long_name": "node northing, model Cartesian (EPSG:26984)"},
    "xc": {"units": "m", "long_name": "element centroid easting (EPSG:26984)"},
    "yc": {"units": "m", "long_name": "element centroid northing (EPSG:26984)"},
    "triangles": {"long_name": "zero-based indices into the returned nodes"},
    "neighbours": {
        "long_name": "element across the edge opposite each vertex, as an index into the "
        "returned elements; -1 on the mesh boundary, -2 outside the subset",
    },
    "on_boundary": {
        "long_name": "node lies on the mesh boundary (coast or open boundary)"
    },
    "element_id": {"long_name": "zero-based element index in the full mesh"},
    "node_id": {"long_name": "zero-based node index in the full mesh"},
}


def _subset_volume(
    ds: xr.Dataset, times: pd.DatetimeIndex, bbox, wanted: pd.DatetimeIndex, url: str
) -> xr.Dataset:
    """The full-depth subset of an opened FVCOM file (see `read_volume`)."""
    from forcingkit.mesh_frames import element_neighbours

    min_lon, min_lat, max_lon, max_lat = bbox
    lonc = np.asarray(ds["lonc"].values, dtype=np.float64)
    latc = np.asarray(ds["latc"].values, dtype=np.float64)
    elems = np.flatnonzero(
        (lonc >= min_lon) & (lonc <= max_lon) & (latc >= min_lat) & (latc <= max_lat)
    )
    if elems.size == 0:
        raise ValueError(f"No NECOFS elements inside {bbox}")

    nv = np.asarray(ds["nv"].values).T.astype(np.int64) - 1  # (element, 3), zero-based
    tri_global = nv[elems]
    nodes, tri_local = np.unique(tri_global, return_inverse=True)
    tri_local = tri_local.reshape(tri_global.shape).astype(np.int32)

    # Neighbours and boundary nodes come from the whole mesh: on the subset alone every element
    # on the subset's edge would look like a boundary element.
    nbe_all = element_neighbours(nv)
    nbe = nbe_all[elems]
    pos = np.clip(np.searchsorted(elems, np.where(nbe >= 0, nbe, 0)), 0, elems.size - 1)
    selected = (nbe >= 0) & (elems[pos] == nbe)
    neighbours = np.where(
        nbe < 0, NO_NEIGHBOUR, np.where(selected, pos, OUTSIDE_SUBSET)
    ).astype(np.int32)
    open_edge = nbe_all < 0
    boundary_nodes = np.unique(
        np.concatenate(
            [nv[open_edge[:, j], (j + 1) % 3] for j in range(3)]
            + [nv[open_edge[:, j], (j + 2) % 3] for j in range(3)]
        )
    )
    on_boundary = np.isin(nodes, boundary_nodes)

    t_idx = times.get_indexer(wanted)
    keep = t_idx >= 0
    if not keep.any():
        raise RuntimeError(
            f"{url} holds {times[0]} to {times[-1]}, none of {wanted[0]} to {wanted[-1]}"
        )
    t_idx, wanted = t_idx[keep], wanted[keep]
    t0, t1 = int(t_idx.min()), int(t_idx.max())
    t_slice, rows = slice(t0, t1 + 1), t_idx - t0

    def at_elements(name):
        return _read_columns(ds[name], "nele", elems, t_slice)[rows]

    def at_nodes(name):
        return _read_columns(ds[name], "node", nodes, t_slice)[rows]

    def static_nodes(name):
        return _read_columns(ds[name], "node", nodes, None)

    def coord(name, idx):
        return np.asarray(ds[name].values, dtype=np.float64)[idx]

    lay, lev, ele, nod = "siglay", "siglev", "element", "node"
    data = {
        "u": (("time", lay, ele), at_elements("u")),
        "v": (("time", lay, ele), at_elements("v")),
        "ww": (("time", lay, ele), at_elements("ww")),
        "temp": (("time", lay, nod), at_nodes("temp")),
        "salinity": (("time", lay, nod), at_nodes("salinity")),
        "zeta": (("time", nod), at_nodes("zeta")),
        "h": ((nod,), static_nodes("h")),
        "siglay": ((lay, nod), static_nodes("siglay")),
        "siglev": ((lev, nod), static_nodes("siglev")),
        "x": ((nod,), coord("x", nodes)),
        "y": ((nod,), coord("y", nodes)),
        "node_lon": ((nod,), coord("lon", nodes)),
        "node_lat": ((nod,), coord("lat", nodes)),
        "xc": ((ele,), coord("xc", elems)),
        "yc": ((ele,), coord("yc", elems)),
        "lonc": ((ele,), lonc[elems]),
        "latc": ((ele,), latc[elems]),
        "triangles": ((ele, "vertex"), tri_local),
        "neighbours": ((ele, "vertex"), neighbours),
        "on_boundary": ((nod,), on_boundary),
        "element_id": ((ele,), elems.astype(np.int64)),
        "node_id": ((nod,), nodes.astype(np.int64)),
    }
    out = xr.Dataset(data, coords={"time": wanted})
    for name, attrs in VOLUME_VARIABLE_ATTRS.items():
        out[name].attrs.update(attrs)
    out.attrs.update(
        {
            "type": "NECOFS/FVCOM GOM7 full-depth subset on the model mesh",
            "source": "UMass Dartmouth SMAST",
            "url": url,
            "requested_bbox": list(bbox),
            "schema": VOLUME_SCHEMA,
            "crs_xy": "EPSG:26984",
        }
    )
    logger.info(
        f"NECOFS volume: {elems.size} elements, {nodes.size} nodes, "
        f"{ds.sizes['siglay']} layers, {len(wanted)} hours from {url}"
    )
    return out


def read_volume(
    bbox: list[float],
    start_time: str | pd.Timestamp,
    hours: int,
    dap_url: str | None = None,
) -> xr.Dataset:
    """Every sigma layer on the FVCOM mesh itself (no regridding, no vertical conversion), for the
    elements whose centres lie in `bbox` = [min_lon, min_lat, max_lon, max_lat] and the nodes of
    those elements, hourly from `start_time` for `hours` (hours the file does not hold are left
    out).

    Returns dims (time, siglay, siglev, element, node, vertex):

    - `u`, `v`, `ww` (time, siglay, element): velocities at element layer centres. `ww` is the
      vertical velocity in z, which FVCOM derives from the sigma velocity omega; omega itself is
      not in the archive.
    - `temp`, `salinity` (time, siglay, node); `zeta` (time, node); `h` (node).
    - `siglay` (siglay, node), `siglev` (siglev, node): sigma, surface first, so the depth of a
      layer centre is `siglay * (h + zeta) + zeta`.
    - `x`, `y`, `xc`, `yc`: the model's own Cartesian coordinates in metres (EPSG:26984);
      `node_lon`, `node_lat`, `lonc`, `latc` in degrees.
    - `triangles` (element, vertex): indices into the returned nodes.
    - `neighbours` (element, vertex): the element across the edge opposite each vertex (FVCOM's
      `NBE` order), as an index into the returned elements; NO_NEIGHBOUR (-1) where the edge is
      on the mesh boundary and OUTSIDE_SUBSET (-2) where the neighbour was not selected. Both are
      found on the whole mesh, so the subset's own edge is never mistaken for the coast.
    - `on_boundary` (node): the node lies on the mesh boundary.
    - `element_id`, `node_id`: indices in the full mesh.

    Reads the rolling GOM7 forecast unless `dap_url` names another file (for example an archive
    day from `get_necofs_url`). For a window that spans several archive files, use `volume`.
    """
    url = dap_url or NECOFS_GOM7_URL
    ds, times = _open_archive(url)
    start = pd.Timestamp(start_time)
    start = start.tz_convert("UTC").tz_localize(None) if start.tzinfo else start
    wanted = pd.date_range(start.floor("h"), periods=hours, freq="1h")
    return _subset_volume(ds, times, bbox, wanted, url)


def volume_cache_key(bbox, start: pd.Timestamp, end: pd.Timestamp) -> str:
    """Cache key of a `volume` request: the schema, the box to 1e-4 degree and the window."""
    import hashlib
    import json

    key = json.dumps(
        {
            "schema": VOLUME_SCHEMA,
            "bbox": [round(float(b), 4) for b in bbox],
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        sort_keys=True,
    )
    return "necofs_volume_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def volume(
    bbox: list[float],
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    cache: bool = True,
    reader=read_volume,
) -> xr.Dataset:
    """`read_volume` for every hour from `start` to `end` inclusive (UTC; naive times are UTC),
    assembled across the daily archive files that hold them.

    Raises rather than returning a short window: every requested hour must be found, and every file
    must select the same elements. With `cache`, the result is stored as a zarr store under
    `cache_dir("necofs", "volume")` and later calls with the same box and window read it from
    there. Pad `bbox` yourself if a margin beyond the area of interest is needed (for example for
    particle tracking near the edge of the subset). `reader` is for tests.
    """
    import os
    import shutil

    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    t0 = t0.tz_convert("UTC").tz_localize(None) if t0.tzinfo else t0
    t1 = t1.tz_convert("UTC").tz_localize(None) if t1.tzinfo else t1
    t0 = t0.floor("h")
    if t1 < t0:
        raise ValueError(f"end {t1} precedes start {t0}")
    wanted = pd.date_range(t0, t1, freq="1h")

    path = None
    if cache:
        path = settings.cache_dir(
            "necofs", "volume", volume_cache_key(bbox, t0, t1) + ".zarr"
        )
        if os.path.isdir(path):
            logger.info(f"NECOFS volume from cache {path}")
            return xr.open_zarr(path, consolidated=True).load()

    groups: list[tuple[str, list[pd.Timestamp]]] = []
    url_by_day: dict[pd.Timestamp, str] = {}
    for t in wanted:
        day = necofs_archive_file_date(t)
        if day not in url_by_day:
            url_by_day[day] = get_necofs_url(day - pd.Timedelta(days=1))
        url = url_by_day[day]
        if groups and groups[-1][0] == url:
            groups[-1][1].append(t)
        else:
            groups.append((url, [t]))

    parts: list[xr.Dataset] = []
    seen: set[pd.Timestamp] = set()
    for url, ts in groups:
        hours = int((ts[-1] - ts[0]) / pd.Timedelta(hours=1)) + 1
        part = reader(bbox, ts[0], hours, dap_url=url)
        if parts and not np.array_equal(
            part["element_id"].values, parts[0]["element_id"].values
        ):
            raise RuntimeError(f"{url} subsets a different set of elements for {bbox}")
        fresh = [
            t
            for t in pd.DatetimeIndex(part["time"].values)
            if t in ts and t not in seen
        ]
        seen.update(fresh)
        if fresh:
            parts.append(part.sel(time=fresh))
    missing = wanted.difference(pd.DatetimeIndex(sorted(seen)))
    if len(missing):
        raise RuntimeError(
            f"NECOFS lacks {len(missing)} of {len(wanted)} hours from {t0} to {t1} "
            f"(first missing {missing[0]})"
        )

    static = [k for k, v in parts[0].data_vars.items() if "time" not in v.dims]
    out = xr.concat([p.drop_vars(static) for p in parts], dim="time").sortby("time")
    out = out.assign({k: parts[0][k] for k in static})
    out.attrs = dict(parts[0].attrs)
    out.attrs.pop("url", None)
    out.attrs["urls"] = [url for url, _ in groups]

    if path is not None:
        tmp = path + ".partial"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        out.to_zarr(tmp, mode="w", consolidated=True, zarr_format=2)
        os.replace(tmp, path)
        logger.info(f"NECOFS volume cached at {path}")
    return out
