import io
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlsplit

import pandas as pd
import requests

from forcingkit import settings

logger = logging.getLogger(__name__)

BASE_ERDDAP_URL = "http://merlin.dms.uconn.edu:8080/erddap"


class ErddapUnavailable(Exception):
    """A dataset was not read, because its host or the dataset failed now or recently."""


class _FailureCache:
    """Hosts and datasets that failed recently, skipped until their entry expires.

    A host is recorded when a connect fails (refused, unreachable or timed out), so the other
    datasets on it, in this request and the next few, are skipped instead of each waiting out the
    connect timeout. A dataset is recorded when its read times out or the server returns a 5xx.
    The lifetime is settings.erddap_failure_ttl(); 0 records nothing.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def record(self, key: str, reason: str) -> None:
        ttl = settings.erddap_failure_ttl()
        if ttl <= 0:
            return
        with self._lock:
            self._entries[key] = (time.monotonic() + ttl, reason)

    def check(self, key: str) -> Optional[str]:
        """The recorded reason while `key` is still failed, else None."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, reason = entry
            if time.monotonic() >= expires:
                del self._entries[key]
                return None
            return reason

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_failures = _FailureCache()


def clear_failure_cache() -> None:
    """Forget every recorded host and dataset failure."""
    _failures.clear()


def _read_dataset(
    dataset_name: str, start_time: str, end_time: str
) -> Optional[pd.DataFrame]:
    """One dataset's temperature series from ERDDAP, indexed by UTC time.

    Returns None when ERDDAP has no rows for the window (HTTP 404). Raises ErddapUnavailable when
    the host or dataset is negatively cached, or when this read fails.
    """
    host = urlsplit(BASE_ERDDAP_URL).netloc
    host_key = f"host:{host}"
    dataset_key = f"dataset:{dataset_name}"
    for key in (host_key, dataset_key):
        reason = _failures.check(key)
        if reason is not None:
            raise ErddapUnavailable(f"skipped after a recent failure: {reason}")

    query_url = f"{BASE_ERDDAP_URL}/tabledap/{dataset_name}.csvp?time,sea_water_temperature&time%3E={start_time}&time%3C={end_time}"
    logger.info(
        f"Fetching ERDDAP data for {dataset_name} ({start_time} to {end_time})..."
    )
    try:
        response = requests.get(query_url, timeout=settings.erddap_timeouts())
    except requests.ConnectionError as e:
        # Includes ConnectTimeout: the host is unreachable, so skip it for a while.
        reason = f"{host} unreachable ({type(e).__name__})"
        _failures.record(host_key, reason)
        raise ErddapUnavailable(reason) from e
    except requests.Timeout as e:
        reason = f"{dataset_name} read timed out"
        _failures.record(dataset_key, reason)
        raise ErddapUnavailable(reason) from e
    except requests.RequestException as e:
        raise ErddapUnavailable(
            f"{dataset_name} request failed ({type(e).__name__})"
        ) from e

    if response.status_code == 404:
        # ERDDAP answers 404 when the query matches no rows.
        logger.info(f"ERDDAP has no rows for {dataset_name} in the window")
        return None
    if response.status_code >= 500:
        reason = f"{dataset_name} returned HTTP {response.status_code}"
        _failures.record(dataset_key, reason)
        raise ErddapUnavailable(reason)
    if response.status_code != 200:
        raise ErddapUnavailable(f"{dataset_name} returned HTTP {response.status_code}")

    try:
        df = pd.read_csv(io.StringIO(response.text))
        if df.empty:
            return None
        # The headers are like "time (UTC)" and "sea_water_temperature (celsius)".
        df.columns = ["time", "sea_water_temperature"]
        df = df.set_index("time")
        df.index = pd.to_datetime(df.index, utc=True)
    except Exception as e:
        raise ErddapUnavailable(f"{dataset_name} could not be parsed ({e})") from e
    return df


def fetch_erddap_station_profiles(
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
    Fetches 3-depth temperature profiles for a given station.
    Targets Long Island Sound stations on the UConn ERDDAP server.

    The three datasets are read concurrently, each with the timeouts of
    settings.erddap_timeouts(). A dataset that fails, or whose host or name failed within
    settings.erddap_failure_ttl() seconds, is left out of the result.

    Args:
        station_id: Station ID, e.g., "WLIS", "EXRX"
        start_time: ISO8601 string (e.g., "YYYY-MM-DDTHH:MM:SSZ")
        end_time: ISO8601 string
        skipped: when given, a dict per dataset left out is appended to it, with keys
            "station", "level", "dataset" and "reason".

    Returns:
        dictionary with keys: "surface", "mid", "bottom", each containing a dictionary of time (ISO) -> temperature (float)
    """
    os.makedirs(cache_dir, exist_ok=True)

    station_upper = station_id.upper()
    datasets = {
        "surface": f"{station_upper}_WQ_SFC",
        "mid": f"{station_upper}_WQ_MID",
        "bottom": f"{station_upper}_WQ_BTM",
    }

    # Handle irregular dataset names in LISICOS
    if station_upper == "ARTG":
        datasets["bottom"] = "ARTG_WQ_BTM_1"

    def load(
        level: str, dataset_name: str
    ) -> tuple[Optional[pd.DataFrame], Optional[dict]]:
        cache_key = f"{dataset_name}_{start_time}_{end_time}".replace(":", "-").replace(
            "Z", ""
        )
        cache_path = os.path.join(cache_dir, f"{cache_key}.csv")

        if not cache_bust and os.path.exists(cache_path):
            logger.info(f"Cache hit for {dataset_name}: {cache_path}")
            return pd.read_csv(cache_path, index_col=0, parse_dates=True), None
        try:
            df = _read_dataset(dataset_name, start_time, end_time)
        except ErddapUnavailable as e:
            logger.error(f"Failed fetching or parsing ERDDAP for {dataset_name}: {e}")
            return None, {
                "station": station_upper,
                "level": level,
                "dataset": dataset_name,
                "reason": str(e),
            }
        if df is not None and not df.empty:
            df.to_csv(cache_path)
        return df, None

    with ThreadPoolExecutor(max_workers=len(datasets)) as pool:
        loaded = list(pool.map(lambda item: (item[0], *load(*item)), datasets.items()))

    profiles = {}
    for level, df, skip in loaded:
        if skip is not None and skipped is not None:
            skipped.append(skip)
        if df is not None and not df.empty:
            df = df.dropna(subset=["sea_water_temperature"])
            # We must convert timestamp back to isoformat strings to match json expectations if returned by API
            time_dict = {
                ts.isoformat(): temp for ts, temp in df["sea_water_temperature"].items()
            }
            profiles[level] = time_dict

    return profiles


KNOWN_STATIONS = {
    "WLIS": {"lat": 40.96, "lon": -73.58},
    "EXRX": {"lat": 40.88, "lon": -73.73},
    "CLIS": {"lat": 41.14, "lon": -72.66},
    "ARTG": {"lat": 41.01, "lon": -73.29},
}

# Stations substituted for a box that contains none of KNOWN_STATIONS, only when a request sets
# allow_proxy_stations. Each substitute is returned with within_bbox False.
PROXY_STATIONS = ("EXRX", "WLIS")


def _distance_to_bbox_km(lat: float, lon: float, bbox: list[float]) -> float:
    """Great-circle distance in km from (lat, lon) to the nearest point of
    bbox [min_lon, min_lat, max_lon, max_lat]; 0 inside it."""
    min_lon, min_lat, max_lon, max_lat = bbox
    near_lat = min(max(lat, min_lat), max_lat)
    near_lon = min(max(lon, min_lon), max_lon)
    phi1, phi2 = math.radians(lat), math.radians(near_lat)
    dphi = phi2 - phi1
    dlmb = math.radians(near_lon - lon)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


def fetch_erddap_stations_in_bbox(
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
    Finds the known Long Island Sound stations within the bounding box and fetches their
    profiles.
    bbox: [min_lon, min_lat, max_lon, max_lat]

    A box that contains no known station returns {} without fetching anything. With
    allow_proxy_stations, it gets PROXY_STATIONS instead. Every returned station carries
    within_bbox and distance_km (0 inside the box), so a substitute is never mistaken for a
    station in range.
    """
    if len(bbox) == 4:
        min_lon, min_lat, max_lon, max_lat = bbox
    else:
        return {}

    in_bbox = [
        st_id
        for st_id, coords in KNOWN_STATIONS.items()
        if min_lat <= coords["lat"] <= max_lat and min_lon <= coords["lon"] <= max_lon
    ]

    stations_to_fetch = in_bbox
    if not in_bbox:
        if not allow_proxy_stations:
            logger.info(f"No known ERDDAP station in bbox {bbox}; returning none.")
            return {}
        logger.warning(
            f"No stations found in bbox {bbox}. Substituting proxy stations "
            f"{', '.join(PROXY_STATIONS)} (allow_proxy_stations), marked within_bbox false"
        )
        stations_to_fetch = list(PROXY_STATIONS)

    results = {}
    for st_id in stations_to_fetch:
        logger.info(f"Fetching station {st_id} for bounding box request...")
        profile = fetch_erddap_station_profiles(
            st_id, start_time, end_time, cache_dir, cache_bust, skipped=skipped
        )
        if profile and any(v for v in profile.values()):
            coords = KNOWN_STATIONS[st_id]
            results[st_id] = {
                "lat": coords["lat"],
                "lon": coords["lon"],
                "profiles": profile,
                "within_bbox": st_id in in_bbox,
                "distance_km": round(
                    _distance_to_bbox_km(coords["lat"], coords["lon"], bbox), 1
                ),
            }

    return results
