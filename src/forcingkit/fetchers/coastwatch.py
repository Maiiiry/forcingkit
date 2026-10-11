"""Gridded satellite surface fields from NOAA CoastWatch ERDDAP servers.

Sea surface temperature, chlorophyll-a, the diffuse attenuation coefficient at 490 nm (Kd490, a water-clarity
proxy) and suspended particulate matter, as xarray Datasets on each product's own grid: dims (time, lat, lon) with
latitude ascending, float32, the product's units, and its source and attribution in the attributes. These are
surface estimates for initial conditions, light attenuation, and validation, and the observed layer of field
products (a turbidity field, chlorophyll maps).

Ocean colour is often missing under cloud (runs of days in a cloudy spell), and near the coast it is biased by land
adjacency, so `composite` keeps each pixel's most recent valid value across several sensors and days, with its age,
and `mask_coast` drops a band of pixels along the shore.

Nothing is cached unless a `cache_dir` is given: a library caller (a web job, a notebook) keeps no state. The
service caches each composite as a Zarr store and prunes stores older than the retention period (see
`prune_cache`), so storage cannot grow without bound.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
import xarray as xr

logger = logging.getLogger(__name__)

PFEG = "https://coastwatch.pfeg.noaa.gov/erddap"
CWNOAA = "https://coastwatch.noaa.gov/erddap"
USER_AGENT = "forcingkit (+https://github.com/lhzn-io/forcingkit)"


@dataclass(frozen=True)
class Product:
    """One gridded product: where it lives, what it holds, and how to credit it."""

    server: str
    dataset: str
    variable: str
    name: str  # the variable's name in the returned Dataset
    units: str
    sensor: str
    resolution_m: float
    descending_lat: bool
    altitude: bool  # the dataset carries a singleton altitude dimension
    attribution: str
    note: str = ""


NOAA_CREDIT = "NOAA NESDIS CoastWatch"
COPERNICUS_CREDIT = (
    "Contains modified Copernicus Sentinel data, processed by NOAA NESDIS CoastWatch"
)

PRODUCTS: dict[str, Product] = {
    "sst_mur": Product(
        PFEG,
        "jplMURSST41",
        "analysed_sst",
        "sst",
        "degC",
        "MUR multi-sensor analysis",
        1000.0,
        False,
        False,
        "NASA JPL MUR SST, via NOAA CoastWatch (PO.DAAC)",
        "Gap-free analysis (optimal interpolation of several satellites); about one day behind.",
    ),
    "chl_viirs_snpp": Product(
        CWNOAA,
        "noaacwNPPVIIRSchlaSectorVYDaily",
        "chlor_a",
        "chlor_a",
        "mg m-3",
        "S-NPP VIIRS",
        750.0,
        True,
        True,
        NOAA_CREDIT,
        "Near real time, rolling 90 days.",
    ),
    "chl_viirs_n20": Product(
        CWNOAA,
        "noaacwN20VIIRSchlaSectorUSDaily",
        "chlor_a",
        "chlor_a",
        "mg m-3",
        "NOAA-20 VIIRS",
        750.0,
        True,
        True,
        NOAA_CREDIT,
    ),
    "chl_viirs_n21": Product(
        CWNOAA,
        "noaacwN21VIIRSchlaSectorUSDaily",
        "chlor_a",
        "chlor_a",
        "mg m-3",
        "NOAA-21 VIIRS",
        750.0,
        True,
        True,
        NOAA_CREDIT,
    ),
    "chl_olci_s3a": Product(
        CWNOAA,
        "noaacwS3AOLCIchlaSectorFIDaily",
        "chlor_a",
        "chlor_a",
        "mg m-3",
        "Sentinel-3A OLCI",
        300.0,
        True,
        True,
        COPERNICUS_CREDIT,
        "Experimental; rolling 90 days.",
    ),
    "kd490_viirs_snpp": Product(
        CWNOAA,
        "noaacwNPPVIIRSkd490SectorVYDaily",
        "kd_490",
        "kd_490",
        "m-1",
        "S-NPP VIIRS",
        750.0,
        True,
        True,
        NOAA_CREDIT,
        "Near real time, rolling 90 days.",
    ),
    "kd490_viirs_n20": Product(
        CWNOAA,
        "noaacwN20VIIRSkd490SectorUSDaily",
        "kd_490",
        "kd_490",
        "m-1",
        "NOAA-20 VIIRS",
        750.0,
        True,
        True,
        NOAA_CREDIT,
    ),
    "kd490_viirs_n21": Product(
        CWNOAA,
        "noaacwN21VIIRSkd490SectorUSDaily",
        "kd_490",
        "kd_490",
        "m-1",
        "NOAA-21 VIIRS",
        750.0,
        True,
        True,
        NOAA_CREDIT,
    ),
    "chl_dineof_2km": Product(
        CWNOAA,
        "noaacwNPPN20S3ASCIDINEOF2kmDaily",
        "chlor_a",
        "chlor_a",
        "mg m-3",
        "VIIRS and Sentinel-3A OLCI, gap-filled (DINEOF)",
        2000.0,
        True,
        True,
        COPERNICUS_CREDIT,
        "Gap-filled; about 12 days behind.",
    ),
    "kd490_dineof_2km": Product(
        CWNOAA,
        "noaacwNPPN20S3AkdSCIDINEOF2kmDaily",
        "kd_490",
        "kd_490",
        "m-1",
        "VIIRS and Sentinel-3A OLCI, gap-filled (DINEOF)",
        2000.0,
        True,
        True,
        COPERNICUS_CREDIT,
        "Gap-filled; about 12 days behind.",
    ),
    "spm_dineof_2km": Product(
        CWNOAA,
        "noaacwNPPN20S3AspmSCIDINEOF2kmDaily",
        "spm",
        "spm",
        "g m-3",
        "VIIRS and Sentinel-3A OLCI, gap-filled (DINEOF)",
        2000.0,
        True,
        True,
        COPERNICUS_CREDIT,
        "Suspended particulate matter (mg/L = g/m3); gap-filled; about 12 days behind.",
    ),
}

# Sensors of one quantity on the same 750 m grid, merged by `composite`.
GROUPS: dict[str, list[str]] = {
    "chlor_a": ["chl_viirs_snpp", "chl_viirs_n20", "chl_viirs_n21"],
    "kd_490": ["kd490_viirs_snpp", "kd490_viirs_n20", "kd490_viirs_n21"],
}

# What the service offers by quantity: the products composited for each.
QUANTITIES: dict[str, list[str]] = {
    "sst": ["sst_mur"],
    "chlor_a": GROUPS["chlor_a"],
    "kd_490": GROUPS["kd_490"],
    "spm": ["spm_dineof_2km"],
}


def _utc(t: datetime | np.datetime64 | str) -> pd.Timestamp:
    """A timestamp in UTC (a naive time is taken as UTC, as decoded CF times are)."""
    ts = pd.Timestamp(t)
    return ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")


def _iso(t: datetime | np.datetime64 | str) -> str:
    return _utc(t).strftime("%Y-%m-%dT%H:%M:%SZ")


def _naive64(t: datetime | np.datetime64 | str) -> np.datetime64:
    return np.datetime64(_utc(t).tz_localize(None), "ns")


def griddap_url(
    product: Product, bbox: list[float], start: datetime, end: datetime
) -> str:
    """The ERDDAP griddap query for a product over `bbox` = [min_lon, min_lat, max_lon, max_lat], as NetCDF-3."""
    min_lon, min_lat, max_lon, max_lat = bbox
    lat = (
        f"[({max_lat}):({min_lat})]"
        if product.descending_lat
        else f"[({min_lat}):({max_lat})]"
    )
    alt = "[(0.0)]" if product.altitude else ""
    q = f"{product.variable}[({_iso(start)}):({_iso(end)})]{alt}{lat}[({min_lon}):({max_lon})]"
    return f"{product.server}/griddap/{product.dataset}.nc?{q}"


def _get(url: str, retries: int = 3) -> requests.Response | None:
    """GET with retries; None when ERDDAP says the request matched no data (a time window with no passes)."""
    delay = 5.0
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=180)
        except requests.RequestException as exc:
            logger.warning(
                "ERDDAP request failed (%s), attempt %d",
                exc.__class__.__name__,
                attempt,
            )
        else:
            if resp.status_code == 200:
                return resp
            body = resp.text[:400].lower()
            if resp.status_code == 404 and (
                "no matching results" in body or "no data" in body
            ):
                return None
            if (
                resp.status_code == 400
                and "outside of the variable's actual_range" in body
            ):
                return None  # the window is outside the rolling archive
            logger.warning(
                "ERDDAP HTTP %d, attempt %d: %s", resp.status_code, attempt, url
            )
            if resp.status_code < 500:
                resp.raise_for_status()
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"ERDDAP: giving up after {retries} attempts: {url}")


def latest_time(product_id: str) -> pd.Timestamp | None:
    """The product's most recent time step (ERDDAP refuses a window that runs past it)."""
    p = PRODUCTS[product_id]
    resp = _get(f"{p.server}/griddap/{p.dataset}.csv?time[(last)]")
    if resp is None:
        return None
    lines = [line for line in resp.text.strip().splitlines() if line]
    return _utc(lines[-1]) if len(lines) >= 3 else None  # header, units row, value


def first_time(product_id: str) -> pd.Timestamp | None:
    """The product's earliest time step (the near-real-time sectors keep a rolling window of a few months)."""
    p = PRODUCTS[product_id]
    resp = _get(f"{p.server}/griddap/{p.dataset}.csv?time[0]")
    if resp is None:
        return None
    lines = [line for line in resp.text.strip().splitlines() if line]
    return _utc(lines[-1]) if len(lines) >= 3 else None


def fetch(
    product_id: str, bbox: list[float], start: datetime, end: datetime
) -> xr.Dataset:
    """A product over `bbox` and `[start, end]`: dims (time, lat, lon), latitude ascending, float32.

    The window is trimmed to the product's latest time step. An empty window returns a Dataset with a zero-length
    time axis rather than raising.
    """
    return _fetch_window(product_id, bbox, start, end, latest_time(product_id))


def _fetch_window(
    product_id: str,
    bbox: list[float],
    start: datetime,
    end: datetime,
    last: pd.Timestamp | None,
) -> xr.Dataset:
    """`fetch` with the product's latest time already known (None: the product has no data)."""
    p = PRODUCTS[product_id]
    if last is not None and _utc(end) > last:
        end = last.to_pydatetime()
    resp = (
        None
        if last is None or _utc(start) > _utc(end)
        else _get(griddap_url(p, bbox, start, end))
    )
    attrs = {
        "product": product_id,
        "dataset": p.dataset,
        "server": p.server,
        "sensor": p.sensor,
        "attribution": p.attribution,
        "resolution_m": p.resolution_m,
        "note": p.note,
    }
    if resp is None:
        empty = xr.DataArray(
            np.zeros((0, 0, 0), dtype=np.float32),
            dims=("time", "lat", "lon"),
            attrs={"units": p.units},
        )
        return xr.Dataset(
            {p.name: empty},
            coords={"time": pd.DatetimeIndex([]), "lat": [], "lon": []},
            attrs=attrs,
        )
    raw = xr.open_dataset(io.BytesIO(resp.content), engine="scipy")
    da = raw[p.variable]
    if "altitude" in da.dims:
        da = da.isel(altitude=0, drop=True)
    da = da.rename({"latitude": "lat", "longitude": "lon"}).sortby("lat")
    da = da.astype(np.float32)
    da.attrs = {
        "units": p.units,
        "long_name": raw[p.variable].attrs.get("long_name", p.name),
    }
    return xr.Dataset({p.name: da}, attrs=attrs)


def composite(
    product_ids: list[str],
    bbox: list[float],
    end: datetime | None = None,
    days: int = 7,
) -> xr.Dataset:
    """Each pixel's most recent valid value over the last `days`, across the given products (sensors of one
    quantity on one grid), with its age in days and which product supplied it.

    Returns a Dataset with `value` (lat, lon), `age_days` (lat, lon, float32, NaN where no pixel was valid) and
    `product_index` (lat, lon, int8 into the `products` attribute, -1 where none), on the first product's grid.
    """
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    frames = []
    used: list[str] = []
    for pid in product_ids:
        try:
            ds = fetch(pid, bbox, start, end)
        except Exception as exc:  # one sensor's outage should not drop the others
            logger.warning("satellite fetch failed for %s: %s", pid, exc)
            continue
        if ds.sizes.get("time", 0) == 0:
            continue
        frames.append((len(used), ds))
        used.append(pid)
    if not frames:
        raise ValueError(
            f"no valid data for {product_ids} in the {days} days to {_iso(end)}"
        )
    base = frames[0][1]
    name = PRODUCTS[used[0]].name
    lat, lon = base["lat"].values, base["lon"].values
    best = np.full((lat.size, lon.size), np.nan, dtype=np.float32)
    best_t = np.full((lat.size, lon.size), np.datetime64("NaT"), dtype="datetime64[ns]")
    source = np.full((lat.size, lon.size), -1, dtype=np.int8)
    for k, ds in frames:
        da = ds[name]
        if not (
            np.array_equal(da["lat"].values, lat)
            and np.array_equal(da["lon"].values, lon)
        ):
            # Same nominal grid with a different extent or float noise: match pixels to the nearest centre.
            dlat = float(np.abs(np.diff(lat)).mean()) if lat.size > 1 else 0.01
            da = da.reindex(lat=lat, lon=lon, method="nearest", tolerance=dlat / 2)
        for ti in range(da.sizes["time"]):
            t = _naive64(da["time"].values[ti])
            v = da.isel(time=ti).values
            newer = np.isfinite(v) & (np.isnat(best_t) | (best_t < t))
            best[newer] = v[newer]
            best_t[newer] = t
            source[newer] = k
    age = (
        (_naive64(end) - best_t).astype("timedelta64[s]").astype(np.float64) / 86400.0
    ).astype(np.float32)
    age[np.isnat(best_t)] = np.nan
    out = xr.Dataset(
        {
            "value": (
                ("lat", "lon"),
                best,
                {"units": base[name].attrs.get("units", ""), "long_name": name},
            ),
            "age_days": (
                ("lat", "lon"),
                age,
                {"units": "days", "long_name": "age of the pixel's value at `end`"},
            ),
            "product_index": (
                ("lat", "lon"),
                source,
                {"long_name": "index into the products attribute; -1 where none"},
            ),
        },
        coords={"lat": lat, "lon": lon},
        attrs={
            "quantity": name,
            "products": ",".join(used),
            "attribution": "; ".join(sorted({PRODUCTS[p].attribution for p in used})),
            "window_days": days,
            "end": _iso(end),
        },
    )
    return out


def fetch_range(
    product_id: str,
    bbox: list[float],
    start: datetime,
    end: datetime,
    chunk_days: int = 10,
) -> xr.Dataset:
    """`fetch` over a long window in chunks of `chunk_days`, so each ERDDAP response stays small (the servers'
    proxies refuse long requests); the chunks are joined on time.

    The product's time range is looked up once, and the window is clamped to it (the near-real-time sectors keep
    only a few months). A chunk that still fails after its retries is logged and skipped, so one bad request costs
    those days rather than the whole series. Raises only when the time range itself cannot be read.
    """
    last = latest_time(product_id)
    first = first_time(product_id) if last is not None else None
    t0 = max(_utc(start), first) if first is not None else _utc(start)
    t1 = min(_utc(end), last) if last is not None else _utc(end)
    parts = []
    while last is not None and t0 <= t1:
        t_end = min(t1, t0 + pd.Timedelta(days=chunk_days) - pd.Timedelta(seconds=1))
        try:
            ds = _fetch_window(
                product_id, bbox, t0.to_pydatetime(), t_end.to_pydatetime(), last
            )
        except Exception as exc:
            logger.warning(
                "satellite chunk skipped for %s, %s to %s: %s",
                product_id,
                _iso(t0),
                _iso(t_end),
                exc,
            )
        else:
            if ds.sizes.get("time", 0):
                parts.append(ds)
        t0 = t_end + pd.Timedelta(seconds=1)
    if not parts:
        return _fetch_window(
            product_id,
            bbox,
            _utc(end).to_pydatetime(),
            _utc(start).to_pydatetime(),
            None,
        )  # empty
    out = xr.concat(parts, dim="time") if len(parts) > 1 else parts[0]
    _, keep = np.unique(out["time"].values, return_index=True)
    return out.isel(time=np.sort(keep))


def composite_series(
    product_ids: list[str],
    bbox: list[float],
    frame_ends: list[datetime],
    days: int = 7,
) -> xr.Dataset:
    """`composite` at each of `frame_ends` (ascending), from one fetch per product: for every frame, each pixel's
    most recent valid value across the products within `days` before the frame's end, with its age in days.

    Returns a Dataset with `value` and `age_days` (time, lat, lon; NaN where no pixel was valid within the window),
    `time` being the frame ends, on the first available product's grid. Sensors that fail are skipped, as in
    `composite`; a frame with no valid pixel is all NaN rather than an error.
    """
    if not frame_ends:
        raise ValueError("no frame ends given")
    ends = [_utc(t) for t in frame_ends]
    if any(b <= a for a, b in zip(ends, ends[1:])):
        raise ValueError("frame_ends must be strictly ascending")
    start = ends[0] - pd.Timedelta(days=days)
    slices: list[tuple[np.datetime64, int, np.ndarray]] = []
    used: list[str] = []
    lat = lon = None
    name = PRODUCTS[product_ids[0]].name
    units = ""
    for pid in product_ids:
        try:
            ds = fetch_range(pid, bbox, start.to_pydatetime(), ends[-1].to_pydatetime())
        except Exception as exc:  # one sensor's outage should not drop the others
            logger.warning("satellite fetch failed for %s: %s", pid, exc)
            continue
        if ds.sizes.get("time", 0) == 0:
            continue
        da = ds[PRODUCTS[pid].name]
        if lat is None or lon is None:
            lat, lon = da["lat"].values, da["lon"].values
            units = da.attrs.get("units", "")
        elif not (
            np.array_equal(da["lat"].values, lat)
            and np.array_equal(da["lon"].values, lon)
        ):
            dlat = float(np.abs(np.diff(lat)).mean()) if lat.size > 1 else 0.01
            da = da.reindex(lat=lat, lon=lon, method="nearest", tolerance=dlat / 2)
        k = len(used)
        used.append(pid)
        for ti in range(da.sizes["time"]):
            slices.append((_naive64(da["time"].values[ti]), k, da.isel(time=ti).values))
    if lat is None or lon is None:
        raise ValueError(
            f"no valid data for {product_ids} between {_iso(start)} and {_iso(ends[-1])}"
        )
    slices.sort(key=lambda item: (item[0], item[1]))
    shape = (lat.size, lon.size)
    best = np.full(shape, np.nan, dtype=np.float32)
    best_t = np.full(shape, np.datetime64("NaT"), dtype="datetime64[ns]")
    values = np.full((len(ends),) + shape, np.nan, dtype=np.float32)
    ages = np.full((len(ends),) + shape, np.nan, dtype=np.float32)
    window = np.timedelta64(days * 86400, "s")
    i = 0
    for f, end in enumerate(ends):
        t_end = _naive64(end)
        while i < len(slices) and slices[i][0] <= t_end:
            t, _, v = slices[i]
            newer = np.isfinite(v) & (np.isnat(best_t) | (best_t <= t))
            best[newer] = v[newer]
            best_t[newer] = t
            i += 1
        age = t_end - best_t
        ok = ~np.isnat(best_t) & (age <= window)
        values[f][ok] = best[ok]
        ages[f][ok] = (
            age[ok].astype("timedelta64[s]").astype(np.float64) / 86400.0
        ).astype(np.float32)
    times = pd.DatetimeIndex([e.tz_localize(None) for e in ends])
    return xr.Dataset(
        {
            "value": (
                ("time", "lat", "lon"),
                values,
                {"units": units, "long_name": name},
            ),
            "age_days": (
                ("time", "lat", "lon"),
                ages,
                {
                    "units": "days",
                    "long_name": "age of the pixel's value at the frame's end",
                },
            ),
        },
        coords={"time": times, "lat": lat, "lon": lon},
        attrs={
            "quantity": name,
            "products": ",".join(used),
            "attribution": "; ".join(sorted({PRODUCTS[p].attribution for p in used})),
            "window_days": days,
        },
    )


def mask_coast(ds: xr.Dataset, pixels: int = 1, var: str = "value") -> xr.Dataset:
    """Drop `pixels` of water along the shore, where land adjacency biases ocean colour.

    Land is taken as pixels never valid in the field (the products already mask land), so the band follows the
    coast the product itself draws.
    """
    from scipy.ndimage import binary_erosion

    if pixels <= 0:
        return ds
    water = np.isfinite(ds[var].values)
    if water.ndim == 3:  # a series: each frame on its own
        keep = np.stack(
            [binary_erosion(w, iterations=pixels, border_value=1) for w in water]
        )
    else:
        keep = binary_erosion(water, iterations=pixels, border_value=1)
    out = ds.copy()
    for name in out.data_vars:
        if out[name].dims == ds[var].dims:
            arr = out[name].values.copy()
            if np.issubdtype(arr.dtype, np.floating):
                arr[~keep] = np.nan
            else:
                arr[~keep] = -1
            out[name] = (out[name].dims, arr, out[name].attrs)
    out.attrs["coast_mask_pixels"] = pixels
    return out


# ---------------------------------------------------------------------------------------------
# Service cache: one Zarr store per request, pruned by age.


def cache_key(
    product_ids: list[str],
    bbox: list[float],
    end: datetime,
    days: int,
    coast_pixels: int,
) -> str:
    """Deterministic store id for a composite (the end time is rounded to the hour, so repeated requests share)."""
    hour = pd.Timestamp(end).floor("h")
    raw = f"{','.join(product_ids)}|{','.join(f'{v:.4f}' for v in bbox)}|{hour}|{days}|{coast_pixels}"
    return "sat_" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def prune_cache(cache_dir: str, max_age_days: float) -> int:
    """Delete satellite stores older than `max_age_days`; returns how many were removed."""
    if not os.path.isdir(cache_dir):
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for entry in os.listdir(cache_dir):
        path = os.path.join(cache_dir, entry)
        if entry.startswith("sat_") and os.path.getmtime(path) < cutoff:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed += 1
    return removed
