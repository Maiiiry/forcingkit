"""HRRR forecasts at points, kept with both times: when each forecast was issued and the time it is for.

For each requested cycle (the issue time) and lead hour, the surface fields are read by byte range from that
cycle's `wrfsfcfNN` file on the anonymous AWS bucket (`noaa-hrrr-bdp-pds`, which holds every run since 2014-07-30),
and the value at each point is taken from the nearest HRRR water cells: an inverse-distance-squared mean of up to
four water cells within `max_km`, so a coastal point never takes a land cell's temperature or wind. Winds are
rotated from the Lambert grid axes to east and north. Values stay in the units HRRR publishes (m/s, K, Pa, m).

The result is one row per (issue_time, valid_time, lead_h, point, field, value): a bitemporal record, so a
forecast's error can later be measured by how far ahead it was made. A lead a cycle does not carry (HRRR runs to
48 h only at 00, 06, 12 and 18 UTC) is skipped; a missing field in a file that exists is an error.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import tempfile
from typing import Iterable

import numpy as np
import pandas as pd

from .hrrr import _fetch_s3_byte_ranges, _parse_idx, fs
from .hrrr_atmosphere import _read_message, earth_relative_winds

logger = logging.getLogger(__name__)

# GRIB variable and level of each field in wrfsfcfNN.
FIELDS = {
    "u10": ("UGRD", "10 m above ground"),
    "v10": ("VGRD", "10 m above ground"),
    "gust": ("GUST", "surface"),
    "t2m": ("TMP", "2 m above ground"),
    "d2m": ("DPT", "2 m above ground"),
    "mslp": ("MSLMA", "mean sea level"),
    "vis": ("VIS", "surface"),
}
DEFAULT_FIELDS = tuple(FIELDS)
UNITS = {
    "u10": "m s-1",
    "v10": "m s-1",
    "gust": "m s-1",
    "t2m": "K",
    "d2m": "K",
    "mslp": "Pa",
    "vis": "m",
}


def s3_key(cycle: pd.Timestamp, lead: int) -> str:
    return f"noaa-hrrr-bdp-pds/hrrr.{cycle.strftime('%Y%m%d')}/conus/hrrr.t{cycle.strftime('%H')}z.wrfsfcf{lead:02d}.grib2"


def idx_pattern(field: str, lead: int) -> str:
    """The .idx text of one field at one lead: "anl" at 0, "N hour fcst" after (instantaneous fields only)."""
    var, level = FIELDS[field]
    return f":{var}:{level}:{'anl' if lead == 0 else f'{lead} hour fcst'}:"


def max_lead(cycle: pd.Timestamp) -> int:
    """The longest lead a cycle carries: 48 h at 00, 06, 12 and 18 UTC (36 h before HRRR v4, 2020-12-02), else 18 h."""
    if cycle.hour % 6:
        return 18
    return 48 if cycle >= pd.Timestamp("2020-12-02") else 36


class PointWeights:
    """Inverse-distance-squared weights from the nearest water cells to each point, built once per grid."""

    def __init__(
        self,
        lon,
        lat,
        land,
        points: dict[str, tuple[float, float]],
        k: int = 4,
        max_km: float = 6.0,
    ):
        lon = ((np.asarray(lon, dtype=float) + 180.0) % 360.0) - 180.0
        lat = np.asarray(lat, dtype=float)
        water = np.asarray(land) < 0.5
        self.names = list(points)
        self.cells: list[np.ndarray] = []
        self.weights: list[np.ndarray] = []
        flat_lon, flat_lat, flat_water = lon.ravel(), lat.ravel(), water.ravel()
        for name in self.names:
            plon, plat = points[name]
            dx = (flat_lon - plon) * 111.32 * np.cos(np.deg2rad(plat))
            dy = (flat_lat - plat) * 110.57
            d = np.hypot(dx, dy)
            d[~flat_water] = np.inf
            near = np.argsort(d)[:k]
            near = near[d[near] <= max_km]
            if not len(near):
                logger.warning(
                    "HRRR points: no water cell within %.0f km of %s", max_km, name
                )
                self.cells.append(np.array([], dtype=int))
                self.weights.append(np.array([]))
                continue
            dist = d[near]
            if dist[0] < 1e-6:  # on a cell centre
                w = np.zeros(len(near))
                w[0] = 1.0
            else:
                w = 1.0 / dist**2
            self.cells.append(near)
            self.weights.append(w / w.sum())

    def __call__(self, field) -> dict[str, float]:
        flat = np.asarray(field, dtype=float).ravel()
        return {
            name: float(np.dot(flat[c], w)) if len(c) else float("nan")
            for name, c, w in zip(self.names, self.cells, self.weights)
        }


def _fetch_lead(cycle: pd.Timestamp, lead: int, fields: Iterable[str], workdir: str):
    """The requested fields (and the land mask) of one cycle and lead, on HRRR's grid; None if the file is absent."""
    key = s3_key(cycle, lead)
    try:
        with fs.open(key + ".idx", "r") as f:
            idx = f.read()
    except FileNotFoundError:
        return None
    patterns = {name: idx_pattern(name, lead) for name in fields} | {
        "land": ":LAND:surface:"
    }
    out = {}
    for name, pattern in patterns.items():
        ranges = _parse_idx(idx, (pattern,))
        if name == "land":
            ranges = ranges[:1]
        if len(ranges) != 1:
            raise FileNotFoundError(
                f"HRRR s3://{key}: expected one message for {pattern!r}, found {len(ranges)}"
            )
        path = os.path.join(
            workdir, f"{name}_{cycle.strftime('%Y%m%d%H')}_f{lead:02d}.grib2"
        )
        _fetch_s3_byte_ranges(key, ranges, path)
        try:
            out[name] = _read_message(path)
        finally:
            os.remove(path)
    return out


_weights_cache: dict = {}


def _weights(native, points, k, max_km) -> PointWeights:
    u = native[next(iter(n for n in native if n != "land"))]
    key = (u.shape, tuple(sorted(points.items())), k, max_km)
    if key not in _weights_cache:
        _weights_cache[key] = PointWeights(
            u["longitude"].values,
            u["latitude"].values,
            native["land"].values,
            points,
            k,
            max_km,
        )
    return _weights_cache[key]


def fetch_points(
    cycles: Iterable,
    leads: Iterable[int],
    points: dict[str, tuple[float, float]],
    fields: Iterable[str] = DEFAULT_FIELDS,
    k: int = 4,
    max_km: float = 6.0,
    max_workers: int = 6,
) -> pd.DataFrame:
    """HRRR forecasts at `points` ({name: (lon, lat)}) for every cycle and lead, as rows of
    (issue_time, valid_time, lead_h, point, field, value). Leads past a cycle's `max_lead` are skipped."""
    fields = tuple(fields)
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown HRRR point fields: {sorted(unknown)}")
    wind = "u10" in fields and "v10" in fields
    tasks = []
    for c in cycles:
        cycle = pd.Timestamp(c)
        if cycle.tzinfo is not None:
            cycle = cycle.tz_convert("UTC").tz_localize(None)
        tasks += [(cycle, int(lead)) for lead in leads if int(lead) <= max_lead(cycle)]
    rows = []
    with (
        tempfile.TemporaryDirectory() as workdir,
        concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex,
    ):
        for (cycle, lead), native in zip(
            tasks, ex.map(lambda t: _fetch_lead(t[0], t[1], fields, workdir), tasks)
        ):
            if native is None:
                logger.info("HRRR %s f%02d not published; skipped", cycle, lead)
                continue
            w = _weights(native, points, k, max_km)
            values = {name: w(native[name].values) for name in fields}
            if wind:
                a = native["u10"].attrs
                if int(a.get("GRIB_uvRelativeToGrid", 1)) == 1:
                    cone = float(np.sin(np.deg2rad(a["GRIB_Latin1InDegrees"])))
                    lov = float(a["GRIB_LoVInDegrees"])
                    for p in points:
                        u, v = earth_relative_winds(
                            np.array([values["u10"][p]]),
                            np.array([values["v10"][p]]),
                            np.array([points[p][0]]),
                            cone,
                            lov,
                        )
                        values["u10"][p], values["v10"][p] = float(u[0]), float(v[0])
            valid = cycle + pd.Timedelta(hours=lead)
            for name in fields:
                for p, v in values[name].items():
                    rows.append((cycle, valid, lead, p, name, v))
    return pd.DataFrame(
        rows, columns=["issue_time", "valid_time", "lead_h", "point", "field", "value"]
    )
