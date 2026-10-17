"""ERDDAP profile fetcher: timeouts, the negative cache, the bbox station test, and the telemetry
routes running off the event loop. The HTTP layer is mocked except for one local socket."""

import asyncio
import socket
import threading
import time

import pytest
import requests
from httpx import ASGITransport, AsyncClient

from forcingkit.fetchers import erddap

START = "2024-05-01T00:00:00Z"
END = "2024-05-01T06:00:00Z"
CSV = (
    "time (UTC),sea_water_temperature (degree_C)\n"
    "2024-05-01T00:00:00Z,10.5\n"
    "2024-05-01T01:00:00Z,10.7\n"
)
# A box off New Jersey, about 110 km south of the nearest known station.
OFFSHORE_NJ = [-73.96183, 39.72442, -73.78396, 39.87757]
# A box around WLIS and EXRX in western Long Island Sound.
WESTERN_SOUND = [-73.80, 40.85, -73.50, 41.00]


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = CSV) -> None:
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def clean_failures(monkeypatch):
    for name in (
        "FORCINGKIT_ERDDAP_CONNECT_TIMEOUT",
        "FORCINGKIT_ERDDAP_READ_TIMEOUT",
        "FORCINGKIT_ERDDAP_FAILURE_TTL",
    ):
        monkeypatch.delenv(name, raising=False)
    erddap.clear_failure_cache()
    yield
    erddap.clear_failure_cache()


@pytest.fixture
def fake_get(monkeypatch):
    """Replace requests.get in the fetcher; `behaviour(url, timeout)` returns or raises."""
    lock = threading.Lock()

    def install(behaviour):
        calls: list[tuple[str, object]] = []

        def get(url, timeout=None):
            with lock:
                calls.append((url, timeout))
            return behaviour(url, timeout)

        monkeypatch.setattr(erddap.requests, "get", get)
        return calls

    return install


def test_reads_use_configured_timeouts(fake_get, monkeypatch, tmp_path):
    monkeypatch.setenv("FORCINGKIT_ERDDAP_CONNECT_TIMEOUT", "2")
    monkeypatch.setenv("FORCINGKIT_ERDDAP_READ_TIMEOUT", "7")
    calls = fake_get(lambda url, timeout: FakeResponse())

    profiles = erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))

    assert set(profiles) == {"surface", "mid", "bottom"}
    assert profiles["surface"]["2024-05-01T00:00:00+00:00"] == 10.5
    assert len(calls) == 3
    assert all(timeout == (2.0, 7.0) for _, timeout in calls)


def test_default_timeouts_are_10_and_60(fake_get, tmp_path):
    calls = fake_get(lambda url, timeout: FakeResponse())
    erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))
    assert {timeout for _, timeout in calls} == {(10.0, 60.0)}


def test_unreachable_host_costs_one_connect_timeout(fake_get, monkeypatch, tmp_path):
    """Three datasets wait out one connect timeout together, and the next station skips the host
    without waiting at all."""
    monkeypatch.setenv("FORCINGKIT_ERDDAP_CONNECT_TIMEOUT", "0.5")

    def unreachable(url, timeout):
        time.sleep(timeout[0])
        raise requests.ConnectTimeout("connect timed out")

    calls = fake_get(unreachable)
    skipped: list = []

    t0 = time.monotonic()
    data = erddap.fetch_erddap_stations_in_bbox(
        WESTERN_SOUND, START, END, str(tmp_path), skipped=skipped
    )
    elapsed = time.monotonic() - t0

    assert data == {}
    assert elapsed < 1.5, elapsed
    # Only the first station's datasets try to connect; the second station skips the host.
    assert calls and all("WLIS" in url for url, _ in calls)
    assert len(skipped) == 6
    assert {s["station"] for s in skipped} == {"WLIS", "EXRX"}
    assert all("unreachable" in s["reason"] for s in skipped)


def test_read_timeout_against_a_silent_server(monkeypatch, tmp_path):
    """A real socket that accepts and never answers: the read gives up after the read timeout."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    accepted = []
    stop = threading.Event()

    def accept_forever():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                accepted.append(server.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept_forever, daemon=True)
    thread.start()
    try:
        port = server.getsockname()[1]
        monkeypatch.setattr(
            erddap, "BASE_ERDDAP_URL", f"http://127.0.0.1:{port}/erddap"
        )
        monkeypatch.setenv("FORCINGKIT_ERDDAP_READ_TIMEOUT", "0.5")
        skipped: list = []

        t0 = time.monotonic()
        profiles = erddap.fetch_erddap_station_profiles(
            "WLIS", START, END, str(tmp_path), skipped=skipped
        )
        elapsed = time.monotonic() - t0
    finally:
        stop.set()
        thread.join()
        for conn in accepted:
            conn.close()
        server.close()

    assert profiles == {}
    assert elapsed < 2.0, elapsed
    assert len(skipped) == 3
    assert all("read timed out" in s["reason"] for s in skipped)


def test_failures_are_negatively_cached(fake_get, tmp_path):
    calls = fake_get(
        lambda url, timeout: (_ for _ in ()).throw(requests.ConnectionError("refused"))
    )

    erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))
    first = len(calls)
    skipped: list = []
    t0 = time.monotonic()
    profiles = erddap.fetch_erddap_station_profiles(
        "WLIS", START, END, str(tmp_path), skipped=skipped
    )

    # The datasets are read concurrently: once one records the host, the rest may skip it.
    assert 1 <= first <= 3
    assert len(calls) == first  # no new request inside the TTL
    assert time.monotonic() - t0 < 0.5
    assert profiles == {}
    assert [s["level"] for s in skipped] == ["surface", "mid", "bottom"]
    assert all("recent failure" in s["reason"] for s in skipped)


def test_negative_cache_expires(fake_get, monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(erddap.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("FORCINGKIT_ERDDAP_FAILURE_TTL", "60")
    calls = fake_get(lambda url, timeout: FakeResponse(status_code=503, text=""))

    erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))
    erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))
    assert len(calls) == 3  # each dataset's 503 is remembered

    clock[0] += 61
    erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))
    assert len(calls) == 6


def test_failure_ttl_zero_turns_the_negative_cache_off(fake_get, monkeypatch, tmp_path):
    monkeypatch.setenv("FORCINGKIT_ERDDAP_FAILURE_TTL", "0")
    calls = fake_get(
        lambda url, timeout: (_ for _ in ()).throw(requests.ConnectionError("refused"))
    )
    erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))
    erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))
    assert len(calls) == 6


def test_no_rows_is_not_a_failure(fake_get, tmp_path):
    calls = fake_get(
        lambda url, timeout: FakeResponse(status_code=404, text="no matching results")
    )
    skipped: list = []
    erddap.fetch_erddap_station_profiles(
        "WLIS", START, END, str(tmp_path), skipped=skipped
    )
    erddap.fetch_erddap_station_profiles(
        "WLIS", START, END, str(tmp_path), skipped=skipped
    )
    assert skipped == []
    assert len(calls) == 6


def test_cached_csv_is_served_while_the_host_is_down(fake_get, tmp_path):
    fake_get(lambda url, timeout: FakeResponse())
    erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))

    calls = fake_get(
        lambda url, timeout: (_ for _ in ()).throw(requests.ConnectionError("down"))
    )
    erddap.fetch_erddap_station_profiles(
        "CLIS", START, END, str(tmp_path)
    )  # marks the host
    profiles = erddap.fetch_erddap_station_profiles("WLIS", START, END, str(tmp_path))

    assert set(profiles) == {"surface", "mid", "bottom"}
    assert calls and all(
        "CLIS" in url for url, _ in calls
    )  # only CLIS reached the network


def test_bbox_with_no_known_station_is_empty_and_fetches_nothing(mocker, tmp_path):
    fetch = mocker.patch("forcingkit.fetchers.erddap.fetch_erddap_station_profiles")
    get = mocker.patch("forcingkit.fetchers.erddap.requests.get")

    assert (
        erddap.fetch_erddap_stations_in_bbox(OFFSHORE_NJ, START, END, str(tmp_path))
        == {}
    )
    fetch.assert_not_called()
    get.assert_not_called()


def test_in_range_stations_keep_their_shape(mocker, tmp_path):
    profile = {"surface": {"2024-05-01T00:00:00+00:00": 10.5}}
    mocker.patch(
        "forcingkit.fetchers.erddap.fetch_erddap_station_profiles", return_value=profile
    )

    data = erddap.fetch_erddap_stations_in_bbox(
        WESTERN_SOUND, START, END, str(tmp_path)
    )

    assert set(data) == {"WLIS", "EXRX"}
    wlis = data["WLIS"]
    assert (wlis["lat"], wlis["lon"], wlis["profiles"]) == (40.96, -73.58, profile)
    assert wlis["within_bbox"] is True
    assert wlis["distance_km"] == 0.0


def test_opt_in_proxy_stations_are_marked(mocker, tmp_path):
    profile = {"surface": {"2024-05-01T00:00:00+00:00": 10.5}}
    fetch = mocker.patch(
        "forcingkit.fetchers.erddap.fetch_erddap_station_profiles", return_value=profile
    )

    data = erddap.fetch_erddap_stations_in_bbox(
        OFFSHORE_NJ, START, END, str(tmp_path), allow_proxy_stations=True
    )

    assert set(data) == set(erddap.PROXY_STATIONS)
    assert fetch.call_count == len(erddap.PROXY_STATIONS)
    for station in data.values():
        assert station["within_bbox"] is False
        assert 100 < station["distance_km"] < 130


def test_distance_to_bbox():
    assert erddap._distance_to_bbox_km(40.0, -73.9, [-74.0, 39.9, -73.8, 40.1]) == 0.0
    # One degree of latitude north of the box is about 111 km.
    assert erddap._distance_to_bbox_km(
        41.0, -73.9, [-74.0, 39.0, -73.8, 40.0]
    ) == pytest.approx(111.2, abs=0.5)


# Routes


def _app():
    from forcingkit_serve.main import app

    return app


def _bbox_body(bbox, **extra):
    min_lon, min_lat, max_lon, max_lat = bbox
    return {
        "bbox": {
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
        },
        "start_time": START,
        "end_time": END,
        **extra,
    }


@pytest.mark.asyncio
async def test_bbox_route_with_no_station_returns_empty(mocker):
    get = mocker.patch("forcingkit.fetchers.erddap.requests.get")
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as ac:
        response = await ac.post("/api/v1/telemetry/bbox", json=_bbox_body(OFFSHORE_NJ))

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "data": {},
        "fallback": False,
        "skipped": [],
    }
    get.assert_not_called()


@pytest.mark.asyncio
async def test_bbox_route_marks_opt_in_fallback(mocker):
    mocker.patch(
        "forcingkit.fetchers.erddap.fetch_erddap_station_profiles",
        return_value={"surface": {"2024-05-01T00:00:00+00:00": 10.5}},
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/api/v1/telemetry/bbox",
            json=_bbox_body(OFFSHORE_NJ, allow_proxy_stations=True),
        )

    body = response.json()
    assert body["fallback"] is True
    assert set(body["data"]) == set(erddap.PROXY_STATIONS)
    assert all(st["within_bbox"] is False for st in body["data"].values())


@pytest.mark.asyncio
async def test_bbox_route_reports_skipped_datasets(mocker, tmp_path):
    from forcingkit.dispatcher import dispatch_bounding_box_profiles_request

    def down(*args, **kwargs):
        raise requests.ConnectionError("refused")

    # Keep the route away from the real CSV cache.
    mocker.patch(
        "forcingkit_serve.main.dispatch_bounding_box_profiles_request",
        side_effect=lambda **kw: dispatch_bounding_box_profiles_request(
            cache_dir=str(tmp_path), **kw
        ),
    )
    mocker.patch("forcingkit.fetchers.erddap.requests.get", side_effect=down)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/api/v1/telemetry/bbox", json=_bbox_body(WESTERN_SOUND)
        )

    body = response.json()
    assert body["data"] == {}
    assert {(s["station"], s["level"]) for s in body["skipped"]} == {
        (st, level) for st in ("WLIS", "EXRX") for level in ("surface", "mid", "bottom")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, target, body",
    [
        (
            "/api/v1/telemetry/bbox",
            "forcingkit_serve.main.dispatch_bounding_box_profiles_request",
            _bbox_body(WESTERN_SOUND),
        ),
        (
            "/api/v1/telemetry/station",
            "forcingkit_serve.main.dispatch_station_profiles_request",
            {"station_id": "WLIS", "start_time": START, "end_time": END},
        ),
        (
            "/api/v1/tide",
            "forcingkit_serve.main.fetch_noaa_tide_data",
            {"station_id": "8467150", "start_time": START, "end_time": END},
        ),
    ],
)
async def test_slow_fetch_does_not_block_other_requests(mocker, path, target, body):
    """While a slow fetch runs, a concurrent health check still answers at once."""

    def slow(**kwargs):
        time.sleep(1.5)
        return {}

    mocker.patch(target, side_effect=slow)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as ac:
        t0 = time.monotonic()

        async def health():
            await asyncio.sleep(0.2)  # let the slow request start first
            response = await ac.get("/health")
            return response, time.monotonic() - t0

        slow_response, (health_response, health_done) = await asyncio.gather(
            ac.post(path, json=body), health()
        )

    assert slow_response.status_code == 200
    assert health_response.status_code == 200
    # Answered while the 1.5 s fetch was still running, not queued behind it.
    assert health_done < 1.0, health_done
