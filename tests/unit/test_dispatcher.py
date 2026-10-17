# We will mock the actual fetchers so we don't hit Copernicus or S3
def test_nyofs_expanded_bbox_covers_offshore_nj():
    """NYOFS domain_bbox min_lat should be 40.2 to accurately restrict its active domain."""
    from forcingkit.fetchers import nyofs

    meta = nyofs.get_metadata()
    assert meta["domain_bbox"][1] >= 40.0, (
        "NYOFS domain_bbox min_lat should be ~40.2 for realistic coverage"
    )


def test_nyofs_rejects_offshore_nj_bbox():
    """NYOFS supports_bbox must REJECT a bbox in the expanded NY Bight region."""
    from forcingkit.fetchers import nyofs

    # A bbox fully within [-74.3, 39.5, -73.3, 41.1]
    bbox = [-74.1, 39.6, -73.5, 39.9]
    assert not nyofs.supports_bbox(bbox)


def test_dispatch_station_profiles_request_wlis(mocker):
    from forcingkit import settings
    from forcingkit.dispatcher import dispatch_station_profiles_request

    mock_fetcher = mocker.patch(
        "forcingkit.fetchers.erddap.fetch_erddap_station_profiles"
    )
    mock_fetcher.return_value = {"surface": {"2024-05-01T00:00:00": 10.0}}

    result = dispatch_station_profiles_request(
        "WLIS", "2024-05-01T00:00:00Z", "2024-05-01T06:00:00Z"
    )
    assert "surface" in result
    mock_fetcher.assert_called_once_with(
        station_id="WLIS",
        start_time="2024-05-01T00:00:00Z",
        end_time="2024-05-01T06:00:00Z",
        cache_dir=settings.cache_dir("erddap"),
        cache_bust=False,
        skipped=None,
    )
