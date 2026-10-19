"""Model output on its native unstructured mesh, as a time series of per-element values.

`MeshFrames` is the model-agnostic contract: node coordinates, triangle connectivity and, for each
time, one value per triangle (element). `mesh_frames(model, bbox, start, end)` builds one from a
registered model adapter. `forcingkit.render` turns it into image frames.

Adding a model
--------------
An adapter is one function ``(bbox, times) -> MeshFrames`` registered under a name::

    from forcingkit.mesh_frames import MeshFrames, register_adapter

    @register_adapter("mymodel")
    def _mymodel(bbox, times):
        # bbox: (min_lon, min_lat, max_lon, max_lat); times: tz-aware UTC DatetimeIndex.
        # Read every element that overlaps bbox (a margin beyond it is fine: the renderer
        # clips) for the requested times; omit any time the source does not hold.
        return MeshFrames(times=..., node_lon=..., node_lat=..., tri=..., u=..., v=...)

The registry builds the requested time axis (``start`` to ``end`` inclusive, every ``stride_h``
hours), so an adapter only reads and reshapes. Triangle meshes from FVCOM, ADCIRC or SCHISM fit
directly: ``tri`` is the zero-based element-to-node table (FVCOM ``nv`` is one-based and
transposed), and element values are the model's own (FVCOM u, v at centroids) or the mean of the
three node values for node-centred models.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BBox = Sequence[float]
Adapter = Callable[[tuple[float, float, float, float], pd.DatetimeIndex], "MeshFrames"]

_ADAPTERS: dict[str, Adapter] = {}


def _to_utc(t) -> pd.Timestamp:
    ts = pd.Timestamp(t)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


@dataclass
class MeshFrames:
    """Per-element values on a triangle mesh, one frame per time.

    times: (t,) tz-aware UTC. Naive input is taken as UTC.
    node_lon, node_lat: (n_node,) degrees.
    tri: (n_tri, 3) zero-based node indices, one row per element.
    u, v: (t, n_tri) eastward and northward value per element (m/s for currents). NaN marks an
        element with no value at that time (dry, masked); it renders as outside the mesh.

    Values are on elements only: one value per triangle, drawn with flat shading.
    """

    times: pd.DatetimeIndex
    node_lon: np.ndarray
    node_lat: np.ndarray
    tri: np.ndarray
    u: np.ndarray
    v: np.ndarray
    attrs: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        times = pd.DatetimeIndex(self.times)
        self.times = (
            times.tz_localize("UTC") if times.tz is None else times.tz_convert("UTC")
        )
        self.node_lon = np.asarray(self.node_lon, dtype=np.float64)
        self.node_lat = np.asarray(self.node_lat, dtype=np.float64)
        self.tri = np.asarray(self.tri, dtype=np.int64)
        self.u = np.asarray(self.u, dtype=np.float32)
        self.v = np.asarray(self.v, dtype=np.float32)
        n_node = self.node_lon.shape[0]
        if self.node_lat.shape != (n_node,) or self.node_lon.ndim != 1:
            raise ValueError("node_lon and node_lat must be 1-D and the same length")
        if self.tri.ndim != 2 or self.tri.shape[1] != 3:
            raise ValueError(f"tri must be (n_tri, 3), got {self.tri.shape}")
        if self.tri.size and (self.tri.min() < 0 or self.tri.max() >= n_node):
            raise ValueError("tri holds node indices outside node_lon/node_lat")
        shape = (len(self.times), self.tri.shape[0])
        if self.u.shape != shape or self.v.shape != shape:
            raise ValueError(
                f"u and v must be (t, n_tri) = {shape}, got {self.u.shape} and {self.v.shape}"
            )

    def __len__(self) -> int:
        return len(self.times)

    def speed(self) -> np.ndarray:
        """(t, n_tri) magnitude of (u, v)."""
        return np.hypot(self.u, self.v)


def register_adapter(name: str) -> Callable[[Adapter], Adapter]:
    """Register `fn(bbox, times) -> MeshFrames` under `name` (see the module docstring)."""

    def deco(fn: Adapter) -> Adapter:
        _ADAPTERS[name.lower()] = fn
        return fn

    return deco


def available_models() -> list[str]:
    """Names accepted by `mesh_frames`."""
    return sorted(_ADAPTERS)


def frame_times(start, end, stride_h: int = 1) -> pd.DatetimeIndex:
    """UTC times from `start` to `end` inclusive every `stride_h` hours, `start` floored to the hour."""
    if stride_h < 1:
        raise ValueError(f"stride_h must be a positive number of hours, got {stride_h}")
    t0, t1 = _to_utc(start).floor("h"), _to_utc(end)
    if t1 < t0:
        raise ValueError(f"end {t1} precedes start {t0}")
    return pd.date_range(t0, t1, freq=f"{int(stride_h)}h")


def mesh_frames(model: str, bbox: BBox, start, end, stride_h: int = 1) -> MeshFrames:
    """Values of `model` on its native mesh for the elements in `bbox` = (W, S, E, N), from `start`
    to `end` inclusive every `stride_h` hours (UTC; naive times are taken as UTC).

    Raises KeyError for an unregistered model. Times the source does not hold are left out of the
    result and logged.
    """
    key = model.lower()
    if key not in _ADAPTERS:
        raise KeyError(
            f"No mesh adapter for {model!r}; available: {available_models()}"
        )
    w, s, e, n = (float(x) for x in bbox)
    if not (e > w and n > s):
        raise ValueError(f"bbox must be (W, S, E, N) with E > W and N > S, got {bbox}")
    times = frame_times(start, end, stride_h)
    frames = _ADAPTERS[key]((w, s, e, n), times)
    missing = times.difference(frames.times)
    if len(missing):
        logger.warning(
            f"{model}: {len(missing)} of {len(times)} requested times unavailable"
        )
    return frames


NECOFS_PAD_DEG = 0.05  # wider than the coarsest GOM7 elements near the coast


@register_adapter("necofs")
def _necofs_surface(
    bbox: tuple[float, float, float, float], times: pd.DatetimeIndex
) -> MeshFrames:
    """NECOFS FVCOM GOM7 top-layer currents at element centroids.

    Each hour is read from the SMAST daily history file that holds it (via `get_necofs_url`, which
    falls back to the rolling forecast); consecutive hours served by the same file are read in one
    request, and the results are concatenated with duplicate times dropped. `read_surface` keeps
    elements whose centroids lie in its box, so the box is widened by NECOFS_PAD_DEG to cover the
    requested one edge to edge.
    """
    from forcingkit.fetchers import necofs

    w, s, e, n = bbox
    p = NECOFS_PAD_DEG
    read_box = [w - p, s - p, e + p, n + p]

    naive = times.tz_convert("UTC").tz_localize(None)
    url_by_day: dict[pd.Timestamp, str] = {}
    groups: list[tuple[str, list[pd.Timestamp]]] = []
    for t in naive:
        day = necofs.necofs_archive_file_date(t)
        if day not in url_by_day:
            url_by_day[day] = necofs.get_necofs_url(day - pd.Timedelta(days=1))
        url = url_by_day[day]
        if groups and groups[-1][0] == url:
            groups[-1][1].append(t)
        else:
            groups.append((url, [t]))

    ref = None
    got_t: list[pd.Timestamp] = []
    got_u: list[np.ndarray] = []
    got_v: list[np.ndarray] = []
    seen: set[pd.Timestamp] = set()
    for url, ts in groups:
        hours = int((ts[-1] - ts[0]) / pd.Timedelta(hours=1)) + 1
        try:
            ds = necofs.read_surface(read_box, ts[0], hours, dap_url=url)
        except RuntimeError as exc:
            logger.warning(f"necofs: skipping {ts[0]} to {ts[-1]}: {exc}")
            continue
        if ref is None:
            ref = ds
        elif not np.array_equal(ds["element_id"].values, ref["element_id"].values):
            raise RuntimeError(f"{url} subsets a different set of elements for {bbox}")
        ds_times = pd.DatetimeIndex(ds["time"].values)
        for t in ts:
            i = ds_times.get_indexer([t])[0]
            if i < 0 or t in seen:
                continue
            seen.add(t)
            got_t.append(t)
            got_u.append(ds["u"].values[i])
            got_v.append(ds["v"].values[i])
    if ref is None or not got_t:
        raise RuntimeError(f"NECOFS holds none of {times[0]} to {times[-1]}")

    order = np.argsort(np.asarray(got_t, dtype="datetime64[ns]"))
    return MeshFrames(
        times=pd.DatetimeIndex([got_t[i] for i in order]),
        node_lon=ref["node_lon"].values,
        node_lat=ref["node_lat"].values,
        tri=ref["triangles"].values,
        u=np.stack([got_u[i] for i in order]),
        v=np.stack([got_v[i] for i in order]),
        attrs={
            "model": "necofs",
            "source": "NECOFS FVCOM GOM7, UMass Dartmouth SMAST",
            "variable": "surface current (top sigma layer)",
            "units": "m s-1",
            "bbox": list(bbox),
        },
    )
