import os
import logging
from typing import TYPE_CHECKING, Optional
import numpy as np
from forcingkit import settings

if TYPE_CHECKING:
    import xarray as xr


logger = logging.getLogger(__name__)


def _domain_area(domain_bbox: list[float]) -> float:
    """Compute approximate domain area in degree^2 from [min_lon, min_lat, max_lon, max_lat]."""
    return (domain_bbox[2] - domain_bbox[0]) * (domain_bbox[3] - domain_bbox[1])


def dispatch_station_profiles_request(
    station_id: str,
    start_time: str,
    end_time: str,
    cache_dir: str = os.path.join(
        settings.cache_dir(),
        "erddap",
    ),
    cache_bust: bool = False,
    skipped: Optional[list] = None,
) -> dict:
    """
    Tiered modality dispatcher for internal nudging profiles.
    Currently routes WLIS directly to the ERDDAP fetcher. Datasets left out (failed or
    negatively cached) are appended to `skipped` when it is given.
    """
    logger.info(
        f"Dispatching station profile request for {station_id} ({start_time} to {end_time})"
    )

    # We could implement a real fallback strategy here, but for now we dispatch straight to our ERDDAP module
    from forcingkit.fetchers.erddap import fetch_erddap_station_profiles

    try:
        profiles = fetch_erddap_station_profiles(
            station_id=station_id,
            start_time=start_time,
            end_time=end_time,
            cache_dir=cache_dir,
            cache_bust=cache_bust,
            skipped=skipped,
        )
        return profiles
    except Exception as e:
        logger.error(f"Failed to fetch profiles for {station_id}: {e}")
        return {}


def dispatch_bounding_box_profiles_request(
    bbox: list[float],
    start_time: str,
    end_time: str,
    cache_dir: str = os.path.join(
        settings.cache_dir(),
        "erddap",
    ),
    cache_bust: bool = False,
    allow_proxy_stations: bool = False,
    skipped: Optional[list] = None,
) -> dict:
    """
    Tiered modality dispatcher for internal nudging profiles across a domain.
    A box with no known station returns {} unless allow_proxy_stations is set; see
    fetch_erddap_stations_in_bbox.
    """
    logger.info(
        f"Dispatching bounded profile request for {bbox} ({start_time} to {end_time})"
    )

    from forcingkit.fetchers.erddap import fetch_erddap_stations_in_bbox

    try:
        profiles = fetch_erddap_stations_in_bbox(
            bbox=bbox,
            start_time=start_time,
            end_time=end_time,
            cache_dir=cache_dir,
            cache_bust=cache_bust,
            allow_proxy_stations=allow_proxy_stations,
            skipped=skipped,
        )
        return profiles
    except Exception as e:
        logger.error(f"Failed to fetch bounded profiles: {e}")
        return {}


def _rank_obc_candidates(bbox: list[float]) -> list[tuple]:
    candidates = []
    for module, fetch_func in get_obc_fetchers():
        if not module.supports_bbox(bbox):
            continue
        meta = module.get_metadata()
        domain_bbox = meta.get("domain_bbox")
        area = _domain_area(domain_bbox) if domain_bbox else float("inf")
        resolution = meta.get("resolution_approx_m", float("inf"))
        candidates.append((area, resolution, module, fetch_func, meta))

    candidates.sort(key=lambda c: (c[0], c[1]))
    ranked = [(c[2], c[3], c[4]) for c in candidates]
    if ranked:
        logger.info(
            f"OBC donor ranking for bbox {bbox}: "
            + " > ".join(
                f"{c[4]['name']} (area={c[0]:.1f}°², ~{c[1]:.0f}m)" for c in candidates
            )
        )
    return ranked


def predict_obc_donor(bbox: list[float]) -> dict:
    ranked = _rank_obc_candidates(bbox)
    if ranked:
        return ranked[0][2]
    return {}


def delivered_obc_donor(zarr_path: str) -> Optional[str]:
    """Id of the donor that wrote the OBC store at `zarr_path`, or None if unreadable.

    Stores record the delivering fetcher module in their `source` attribute (for example
    `forcingkit.fetchers.necofs`); this can differ from `predict_obc_donor` after a fallback.
    """
    import zarr

    try:
        source = zarr.open_group(zarr_path, mode="r", zarr_format=2).attrs.get("source")
    except Exception:
        return None
    if not isinstance(source, str) or not source:
        return None
    return source.rsplit(".", 1)[-1]


def dispatch_obc_request(
    start_date: str,
    duration_hours: int,
    bbox: list[float],
    cache_dir: str = settings.cache_dir(),
    cache_bust: bool = False,
    zarr_path: Optional[str] = None,
    allow_donor_fallback: bool = True,
    include_tides: bool = True,
    tidal_model: str = "GOT4.10c",
    sponge_cells: int = 0,
    pad_cells: int = 3,
    vertical_spacing_m: float = 2.0,
) -> str:
    from forcingkit.fetchers.necofs import OBC_SCHEMA

    os.makedirs(cache_dir, exist_ok=True)
    obc_cache_dir = os.path.join(cache_dir, "obc")
    os.makedirs(obc_cache_dir, exist_ok=True)

    import hashlib
    import json

    if not zarr_path:
        key_str = f"obc_{start_date}_{duration_hours}_{bbox[0]}_{bbox[1]}_{bbox[2]}_{bbox[3]}_s{sponge_cells}_{include_tides}_{tidal_model}_{OBC_SCHEMA}_p{pad_cells}_dz{vertical_spacing_m}"
        key_hash = hashlib.sha256(key_str.encode()).hexdigest()[:12]
        zarr_name = f"obc_{key_hash}.zarr"
        zarr_path = os.path.join(obc_cache_dir, zarr_name)

        # Write sidecar provenance
        sidecar_path = os.path.join(obc_cache_dir, f"obc_{key_hash}.json")
        if not os.path.exists(sidecar_path) or cache_bust:
            try:
                with open(sidecar_path, "w") as f:
                    json.dump(
                        {
                            "id": key_hash,
                            "type": "open_boundary_conditions",
                            "start_date": start_date,
                            "duration_hours": duration_hours,
                            "bbox": bbox,
                            "sponge_cells": sponge_cells,
                            "include_tides": include_tides,
                            "tidal_model": tidal_model,
                            "schema": OBC_SCHEMA,
                            "pad_cells": pad_cells,
                            "vertical_spacing_m": vertical_spacing_m,
                        },
                        f,
                        indent=2,
                    )
            except Exception:
                pass

    from forcingkit.zarr_stream import store_is_complete

    if not cache_bust and store_is_complete(zarr_path, (OBC_SCHEMA,)):
        logger.info(f"Cache hit for OBC: {zarr_path}")
        return zarr_path

    ranked = _rank_obc_candidates(bbox)
    if not ranked:
        raise ValueError(f"No suitable OBC fetcher found for bbox {bbox}")

    ds = None
    target_module = None
    meta = None
    candidates_to_try = ranked if allow_donor_fallback else ranked[:1]
    for candidate_module, candidate_fetch_func, candidate_meta in candidates_to_try:
        logger.info(f"Trying OBC donor: {candidate_meta['name']}")
        # A donor that can deliver hour by hour is streamed straight to disk and published only
        # when complete; a failure there is an error, never a shorter store.
        if hasattr(candidate_module, "iter_parent"):
            try:
                return _stream_parent(
                    candidate_module,
                    zarr_path,
                    start_date,
                    duration_hours,
                    bbox,
                    pad_cells=pad_cells,
                    vertical_spacing_m=vertical_spacing_m,
                    sponge_cells=sponge_cells,
                )
            except Exception as e:
                if not allow_donor_fallback:
                    raise RuntimeError(
                        f"{candidate_meta['name']} parent delivery failed: {e}"
                    ) from e
                logger.warning(f"{candidate_meta['name']} parent delivery failed: {e}")
                continue
        try:
            ds = candidate_fetch_func(
                start_date,
                duration_hours,
                bbox,
                **_supported_kwargs(
                    candidate_fetch_func,
                    pad_cells=pad_cells,
                    vertical_spacing_m=vertical_spacing_m,
                ),
            )
        except Exception as e:
            logger.warning(f"{candidate_meta['name']} OBC fetch raised: {e}")
            ds = None
        if ds is not None:
            target_module = candidate_module
            meta = candidate_meta
            logger.info(f"OBC donor succeeded: {meta['name']}")
            break
        if allow_donor_fallback:
            logger.warning(
                f"{candidate_meta['name']} OBC fetch returned no data, trying next donor."
            )

    if ds is None or target_module is None:
        tried = [c[2]["name"] for c in candidates_to_try]
        raise RuntimeError(
            f"Primary OBC donor {tried[0]} failed to return data."
            if not allow_donor_fallback
            else f"All OBC donors failed for bbox {bbox}. Tried: {tried}"
        )

    # REQ-3.1: Temporal Harmonization - Resample to hourly
    logger.info("Aligning OBC data to shared hourly temporal index...")
    if "time" in ds.dims:
        import pandas as pd

        if not isinstance(ds.indexes["time"], pd.DatetimeIndex):
            # Attempt to convert cftime or object arrays to DatetimeIndex
            try:
                ds["time"] = ds.indexes["time"].to_datetimeindex()
            except AttributeError:
                ds["time"] = pd.to_datetime(ds.indexes["time"].values)
        ds = _resample_hourly(ds)

    # REQ-1.3: Tidal boundary condition integration via GOT4.10c or EOT20 (pyTMD).

    # Cast strictly to Float32 for Oceananigans
    for var in ds.data_vars:
        if ds[var].dtype != np.float32:
            ds[var] = ds[var].astype(np.float32)

    # Convert Endianness for Julia
    for var in list(ds.variables):
        ds[var].encoding.clear()
        if ds[var].dtype.kind in "iu":
            ds[var].encoding["_FillValue"] = -9999
        else:
            ds[var].encoding["_FillValue"] = -9999.0

        # Coordinates keep full precision: Float32 longitudes near -74 resolve only about
        # 8e-6 degrees, enough to misplace a 0.002 degree grid relative to its child.
        if (
            (ds[var].dtype == "float64" or ds[var].dtype == "float32")
            and "time" not in str(var)
            and var not in _FULL_PRECISION_COORDS
        ):
            ds[var] = ds[var].astype("<f4")

    # Schema-versioned parent stores state their time axis in seconds from the first record.
    if ds.attrs.get("schema") == OBC_SCHEMA and "time" in ds.coords:
        import pandas as pd

        first = pd.Timestamp(ds["time"].values[0]).strftime("%Y-%m-%dT%H:%M:%S")
        ds["time"].encoding = {"units": f"seconds since {first}", "dtype": "float64"}

    ds.attrs["source"] = target_module.__name__
    ds.attrs["type"] = "3D Time-Varying Hindcast (Coupled Tides + Reanalysis)"
    ds.attrs["duration_hours"] = duration_hours
    ds.attrs["sponge_cells"] = sponge_cells
    ds.attrs["schema"] = ds.attrs.get("schema", "legacy")

    logger.info(f"Writing OBC data to Zarr: {zarr_path}")
    ds.to_zarr(zarr_path, mode="w", consolidated=True, zarr_format=2)
    return zarr_path


_FULL_PRECISION_COORDS = ("lat", "lon", "z", "z_face")


def _resample_hourly(ds: "xr.Dataset") -> "xr.Dataset":
    """Linearly interpolate every time-dependent variable onto an hourly axis.

    Variables may arrive at different cadences on one time axis: HYCOM ESPC-D-V02 has hourly
    surface elevation and 3-hourly currents, temperature and salinity, which are NaN at the
    hours in between. Each variable is therefore interpolated from the times at which it has
    any data, so a coarser field is bridged between its own steps and a finer one keeps its
    native values. A single `resample().interpolate()` would treat those NaNs as data.
    """
    import pandas as pd

    times = ds.indexes["time"]
    hourly = pd.date_range(times.min().floor("h"), times.max().floor("h"), freq="h")
    out = ds.drop_dims("time").assign_coords(time=hourly)
    for name, da in ds.data_vars.items():
        if "time" not in da.dims:
            continue
        others = [d for d in da.dims if d != "time"]
        valid = (da.notnull().any(others) if others else da.notnull()).values
        da = da.isel(time=valid)
        out[name] = (
            da.interp(time=hourly, assume_sorted=True)
            if da.sizes["time"] > 1
            else da.reindex(time=hourly)
        )
    return out[list(ds.data_vars)]


def atmosphere_key(
    bbox: list[float],
    start_time: str,
    hours: int,
    resolution_deg: float,
    margin_deg: float,
    source: str = "hrrr",
) -> str:
    """Cache id of an atmosphere delivery; the schema is part of it."""
    import hashlib

    from forcingkit.fetchers.hrrr_atmosphere import HRRR_ATM_SCHEMA

    key = (
        f"atm_{source}_{HRRR_ATM_SCHEMA}_{bbox[0]}_{bbox[1]}_{bbox[2]}_{bbox[3]}_"
        f"{start_time}_{hours}_{resolution_deg}_{margin_deg}"
    )
    return "atm_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def dispatch_atmosphere_request(
    bbox: list[float],
    start_time: str,
    hours: int,
    zarr_path: str,
    resolution_deg: float = 0.03,
    margin_deg: float = 0.25,
    cache_bust: bool = False,
) -> str:
    """HRRR prescribed atmosphere for `hours` from `start_time`, streamed to `zarr_path`."""
    from forcingkit.fetchers import hrrr_atmosphere
    from forcingkit.zarr_stream import StreamingZarrWriter, store_is_complete

    if not cache_bust and store_is_complete(
        zarr_path, (hrrr_atmosphere.HRRR_ATM_SCHEMA,)
    ):
        logger.info(f"Cache hit for atmosphere: {zarr_path}")
        return zarr_path

    writer = None
    try:
        for item in hrrr_atmosphere.iter_atmosphere(
            start_time,
            hours,
            bbox,
            resolution_deg=resolution_deg,
            margin_deg=margin_deg,
        ):
            if item[0] == "static":
                writer = StreamingZarrWriter(
                    zarr_path,
                    item[1],
                    hrrr_atmosphere.RECORD_DIMS,
                    expected_records=hours + 3,
                    attrs=dict(item[1].attrs),
                    record_attrs={
                        k: {"units": u, "standard_name": sn}
                        for k, (u, sn) in hrrr_atmosphere.UNITS.items()
                    },
                )
                continue
            if writer is None:
                raise RuntimeError(
                    "atmosphere records arrived before the static fields"
                )
            writer.append(item[1], item[2])
        if writer is None:
            raise RuntimeError("HRRR yielded no data")
        return writer.close()
    except Exception:
        if writer is not None:
            writer.abort()
        raise


PROVISIONAL_MAX_AGE_HOURS = 24


def river_mouths(bbox: list[float], mouths: Optional[list[dict]] = None) -> list[dict]:
    """The mouths a river request covers: those given, else the listed mouths in `bbox`."""
    from forcingkit.fetchers.usgs_rivers import (
        load_mouths,
        mouths_in_bbox,
        validate_mouth,
    )

    if mouths is not None:
        return [validate_mouth(dict(m)) for m in mouths]
    return mouths_in_bbox(load_mouths(), bbox)


def river_key(
    bbox: list[float],
    start_time: str,
    hours: int,
    max_gap_hours: int,
    allow_climatology: bool,
    mouths: list[dict],
    min_share: float,
    skip_ungauged: bool = False,
) -> str:
    """Cache id of a river delivery. The schema, the mouth entries and the derivation settings
    are part of it; the derived gauges are not, and are recorded in the store instead."""
    import hashlib

    from forcingkit.fetchers.usgs_rivers import RIVER_SCHEMA, mouths_version

    key = (
        f"riv_{RIVER_SCHEMA}_{mouths_version(mouths)}_{bbox[0]}_{bbox[1]}_{bbox[2]}_"
        f"{bbox[3]}_{start_time}_{hours}_g{max_gap_hours}_c{int(allow_climatology)}_"
        f"s{min_share}_k{int(skip_ungauged)}"
    )
    return "riv_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _provisional_store_is_stale(zarr_path: str) -> bool:
    """True for a store built from provisional USGS values more than a day ago: USGS revises
    provisional values, so such a store is rebuilt rather than served."""
    import pandas as pd
    import zarr

    attrs = dict(zarr.open_group(zarr_path, mode="r", zarr_format=2).attrs)
    if not attrs.get("provisional", False):
        return False
    built = pd.Timestamp(attrs.get("built_utc", "1970-01-01T00:00:00Z"))
    age = pd.Timestamp.now(tz="UTC") - built
    return age > pd.Timedelta(hours=PROVISIONAL_MAX_AGE_HOURS)


def dispatch_river_request(
    bbox: list[float],
    start_time: str,
    hours: int,
    zarr_path: str,
    max_gap_hours: int = 6,
    allow_climatology: bool = False,
    cache_bust: bool = False,
    get=None,
    mouths: Optional[list[dict]] = None,
    min_share: Optional[float] = None,
    skip_ungauged: bool = False,
    rivers: Optional[list[dict]] = None,
) -> str:
    """Hourly discharge at each river mouth (`mouths`, else the listed mouths in `bbox`),
    streamed to `zarr_path`. `rivers` passes already-derived rivers (tests)."""
    import json

    import pandas as pd

    from forcingkit.fetchers import usgs_rivers
    from forcingkit.zarr_stream import StreamingZarrWriter, store_is_complete

    if (
        not cache_bust
        and store_is_complete(zarr_path, (usgs_rivers.RIVER_SCHEMA,))
        and not _provisional_store_is_stale(zarr_path)
    ):
        logger.info(f"Cache hit for rivers: {zarr_path}")
        return zarr_path

    share = usgs_rivers.DEFAULT_MIN_SHARE if min_share is None else min_share
    resolved = None if rivers is not None else river_mouths(bbox, mouths)
    writer = None
    sidecar: dict = {}
    try:
        for item in usgs_rivers.iter_rivers(
            start_time,
            hours,
            bbox,
            max_gap_hours=max_gap_hours,
            allow_climatology=allow_climatology,
            mouths=resolved,
            min_share=share,
            skip_ungauged=skip_ungauged,
            rivers=rivers,
            get=get,
        ):
            if item[0] == "static":
                attrs = dict(item[1].attrs)
                attrs["built_utc"] = pd.Timestamp.now(tz="UTC").strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                writer = StreamingZarrWriter(
                    zarr_path,
                    item[1],
                    usgs_rivers.RECORD_DIMS,
                    expected_records=hours + 1,
                    attrs=attrs,
                    record_attrs={
                        k: {"units": u, "standard_name": sn}
                        for k, (u, sn) in usgs_rivers.UNITS.items()
                    },
                )
                sidecar = {
                    "id": os.path.basename(zarr_path).removesuffix(".zarr"),
                    "type": "river_discharge",
                    "bbox": list(bbox),
                    "start_time": start_time,
                    "hours": hours,
                    "max_gap_hours": max_gap_hours,
                    "allow_climatology": allow_climatology,
                    "schema": usgs_rivers.RIVER_SCHEMA,
                    "min_share": share,
                    "skip_ungauged": skip_ungauged,
                    "skipped_rivers": json.loads(attrs["skipped_rivers"]),
                    "mouths": resolved,
                    "rivers": json.loads(attrs["provenance"]),
                    "dropped": json.loads(attrs["derivation_dropped"]),
                }
                continue
            if writer is None:
                raise RuntimeError("river records arrived before the static fields")
            writer.append(item[1], item[2])
        if writer is None:
            raise RuntimeError("the river fetcher yielded no data")
        path = writer.close()
        try:
            with open(path.removesuffix(".zarr") + ".json", "w") as f:
                json.dump(sidecar, f, indent=2)
        except OSError as e:
            logger.warning(f"Could not write the river sidecar for {path}: {e}")
        return path
    except Exception:
        if writer is not None:
            writer.abort()
        raise


def _stream_parent(
    module,
    zarr_path: str,
    start_date: str,
    duration_hours: int,
    bbox: list[float],
    pad_cells: int,
    vertical_spacing_m: float,
    sponge_cells: int,
) -> str:
    """Write a donor's `iter_parent` records to `zarr_path` one hour at a time."""
    from forcingkit.zarr_stream import StreamingZarrWriter

    writer = None
    try:
        for item in module.iter_parent(
            start_date, duration_hours, bbox, pad_cells, vertical_spacing_m
        ):
            if item[0] == "static":
                static = item[1]
                attrs = dict(static.attrs)
                attrs.update(
                    source=module.__name__,
                    duration_hours=duration_hours,
                    sponge_cells=sponge_cells,
                )
                writer = StreamingZarrWriter(
                    zarr_path,
                    static,
                    module.PARENT_RECORD_DIMS,
                    expected_records=duration_hours,
                    attrs=attrs,
                    record_attrs={
                        k: {"units": u, "standard_name": sn}
                        for k, (u, sn) in module.PARENT_UNITS.items()
                    },
                )
                continue
            _, t, record = item
            if writer is None:
                raise RuntimeError("parent records arrived before the static fields")
            writer.append(t, record)
        if writer is None:
            raise RuntimeError("the donor yielded no data")
        return writer.close()
    except Exception:
        if writer is not None:
            writer.abort()
        raise


def _supported_kwargs(func, **kwargs) -> dict:
    """The subset of `kwargs` that `func` accepts, so options for one donor's fetcher can be
    offered to every candidate without breaking the shared (start, duration, bbox) signature."""
    import inspect

    params = inspect.signature(func).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def get_obc_fetchers():
    from forcingkit.fetchers import (
        dbofs,
        hycom,
        necofs,
        nyofs,
    )

    # Dynamically discover fetch_xxx_boundary_conditions functions.
    # Not all modules have implemented OBC yet, so we check with hasattr.
    fetchers = []
    for module in [nyofs, dbofs, necofs, hycom]:
        func_name = "fetch_" + module.__name__.split(".")[-1] + "_boundary_conditions"
        if hasattr(module, func_name):
            fetchers.append((module, getattr(module, func_name)))
    return fetchers
