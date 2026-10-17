Data Fetchers
=============

A fetcher (``forcingkit.fetchers``) talks to one external provider: it resolves where the data
for a time lives, reads only the subset a request needs, and returns it in a common layout. The
dispatcher (``forcingkit.dispatcher``) chooses among fetchers, falls back when one cannot deliver,
and writes the result to the local Zarr cache, so a request is fetched from the provider once.

Atmospheric forcing (``/api/v1/atmosphere``)
--------------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 14 86

   * - Source
     - Notes
   * - **HRRR**
     - NOAA `High-Resolution Rapid Refresh <https://rapidrefresh.noaa.gov/hrrr/>`__ (developed by
       the `NOAA Global Systems Laboratory <https://gsl.noaa.gov/>`__, run operationally by
       `NCEP <https://www.nco.ncep.noaa.gov/>`__), 3 km, from the anonymous AWS S3 bucket
       `noaa-hrrr-bdp-pds <https://registry.opendata.aws/noaa-hrrr-pds/>`__; the archive starts
       2014-07-30. Surface fields are interpolated to a regular 0.03 degree grid and streamed hour
       by hour (schema ``hrrr-atm-v1``). Earlier dates use `ERA5
       <https://cds.climate.copernicus.eu/>`__ through `NumericalEarth
       <https://github.com/NumericalEarth/NumericalEarth.jl>`__ in the model, outside this
       service. See :doc:`atmospheric_forcing`.

Parent ocean (``/api/v1/obc``)
------------------------------

.. list-table::
   :header-rows: 1
   :widths: 14 86

   * - Source
     - Notes
   * - **NECOFS**
     - `Northeast Coastal Ocean Forecast System <https://fvcom.smast.umassd.edu/?p=20>`__, `FVCOM
       <https://fvcom.smast.umassd.edu/>`__ GOM7 (`UMass Dartmouth SMAST
       <https://www.umassd.edu/smast/>`__ and `WHOI <https://www.whoi.edu/>`__), unstructured mesh
       with 45 sigma layers. Daily history files from 2025-01-01, then the rolling forecast.
       The only donor that streams the ``z-v3`` parent store. See :doc:`necofs`.
   * - **NYOFS**
     - `NOAA New York/New Jersey Operational Forecast System
       <https://tidesandcurrents.noaa.gov/ofs/nyofs/nyofs.html>`__ (`NOAA CO-OPS
       <https://tidesandcurrents.noaa.gov/>`__), POM on a curvilinear grid with 7
       sigma levels, NY/NJ Harbor. Ranked first inside its domain. 7-day CO-OPS aggregation,
       then the nowcast archive (AWS S3 from 2024-11-19, NCEI from 2014). Legacy output layout
       rather than ``z-v3``. See :doc:`nyofs`.
   * - **DBOFS**
     - `NOAA Delaware Bay Operational Forecast System
       <https://tidesandcurrents.noaa.gov/ofs/dbofs/dbofs.html>`__ (NOAA CO-OPS),
       `ROMS <https://www.myroms.org/>`__, about 100 m, domain
       [-75.875, 37.810, -73.264, 40.206]. 7-day CO-OPS aggregation, then the hourly nowcast
       archive (AWS S3 from 2024-11-19, NCEI from 2014). Legacy output layout.
   * - :doc:`HYCOM <hycom>`
     - `HYCOM <https://www.hycom.org/>`__ (1/12 degree, about 9 km, 40 z levels), global, from
       the `HYCOM consortium data server <https://tds.hycom.org/thredds/catalog.html>`__, 1994 to
       the present. The experiment follows the date: GLBv0.08 ``expt_53.X`` (reanalysis) to
       2015, a chain of GLBv0.08 analysis experiments to 2018-12-04, GLBy0.08 ``expt_93.0`` to
       2024-09-05, then ESPC-D-V02. A window that spans a switch is stitched; if any piece fails,
       the whole window fails. Tidal only from 2024-09-05, recorded in the store's ``tides``
       attribute (``"none"``, ``"included"`` or ``"mixed"``). Last-resort fallback; legacy
       output layout.

`TPXO10 <https://www.tpxo.net/>`__ tidal harmonics are a roadmap item, not integrated; the
`pyTMD <https://github.com/pyTMD/pyTMD>`__ dependency was removed on
2026-10-05. The ``include_tides`` and ``tidal_model`` request fields
are still accepted and form part of the cache key, but no tide is added to the parent store.
A parent that already contains the tide (a HYCOM store with ``tides = "included"``) should not
have tides added again downstream; see :doc:`hycom`.

Donor selection
~~~~~~~~~~~~~~~

Each parent-ocean fetcher declares a domain box and an approximate resolution. A donor is a
candidate when it accepts the request box (NYOFS and DBOFS require the box to lie inside their
domain; NECOFS accepts any overlap; HYCOM accepts everything). Candidates are ranked by **domain
area first, smallest first**, then by resolution, on the reasoning that the most specialised
system is the best local one:

.. list-table::
   :header-rows: 1
   :widths: 20 30 20 30

   * - Donor
     - Declared domain (lon/lat)
     - Area (degree squared)
     - Declared resolution
   * - NYOFS
     - [-74.475, 40.389, -73.743, 40.940]
     - 0.40
     - 100 m
   * - DBOFS
     - [-75.875, 37.810, -73.264, 40.206]
     - 6.3
     - 100 m
   * - NECOFS
     - [-77.0, 35.0, -65.0, 46.0]
     - 132
     - 200 m
   * - HYCOM
     - global
     - 64800
     - 9 km

The dispatcher tries the candidates in order. A donor that returns nothing or raises is logged
and skipped unless the request sets ``allow_donor_fallback: false``, in which case the first
failure is an error. A donor that streams (NECOFS) either completes its store or fails; it never
publishes a shortened one.

Observations
------------

These serve validation rather than forcing:

- **CO-OPS** water level (``/api/v1/tide``), from the `NOAA CO-OPS data API
  <https://api.tidesandcurrents.noaa.gov/api/prod/>`__, with a datum
  fallback to MSL where a station has no NAVD88 datum.
- **NDBC** buoy meteorology and ADCP profiles (``/api/v1/ndbc``), from the `NOAA National Data
  Buoy Center <https://www.ndbc.noaa.gov/>`__.
- **UConn ERDDAP** water-column profiles (``/api/v1/telemetry/station`` and ``/bbox``), from the
  `University of Connecticut Department of Marine Sciences <https://marinesciences.uconn.edu/>`__
  `ERDDAP server <http://merlin.dms.uconn.edu:8080/erddap/index.html>`__.
  The station list holds four Long Island Sound moorings, so a box elsewhere contains none and
  ``/bbox`` returns empty ``data``. Setting ``"allow_proxy_stations": true`` substitutes EXRX and
  WLIS instead, for testing; every station carries ``within_bbox`` and ``distance_km`` (0 inside
  the box), and the response's ``fallback`` is true when any lies outside it. Each dataset read
  has a 10 s connect and 60 s read timeout (``FORCINGKIT_ERDDAP_CONNECT_TIMEOUT``,
  ``FORCINGKIT_ERDDAP_READ_TIMEOUT``). A host that fails to connect, or a dataset that times out
  or errors, is skipped for 300 s (``FORCINGKIT_ERDDAP_FAILURE_TTL``, 0 to turn off), and the
  response's ``skipped`` lists each dataset left out, with the reason.

NOAA OFS archive locations
--------------------------

As checked on 2026-10-06, NYOFS and DBOFS output is available from three places:

.. list-table::
   :header-rows: 1
   :widths: 22 30 48

   * - Location
     - Span
     - Layout
   * - `CO-OPS THREDDS <https://opendap.co-ops.nos.noaa.gov/thredds/catalog/catalog.html>`__ FMRC
       aggregation
     - Rolling 7 days
     - One virtual OPeNDAP dataset per system
       (``opendap.co-ops.nos.noaa.gov/thredds/dodsC/<OFS>/fmrc/Aggregated_7_day_<OFS>_Fields_Forecast_best.ncd``).
   * - `NOAA NCEI <https://www.ncei.noaa.gov/>`__ THREDDS (`model-nyofs-files
       <https://www.ncei.noaa.gov/thredds/catalog/model-nyofs-files/catalog.html>`__,
       ``model-dbofs-files``)
     - 2014 to the present month, with occasional missing days (NYOFS 2024-06 has 25 of 30)
     - One directory per month, ``{yyyy}/{mm}/``, with every file of the month in it. File names
       changed with the 2024-09-09 cycles (from ``nos.<ofs>.fields.<n|f><hhh>.<yyyymmdd>.t<cc>z.nc``
       and ``nos.nyofs.fields.<nowcast|forecast>.<yyyymmdd>.t<cc>z.nc`` to
       ``<ofs>.t<cc>z.<yyyymmdd>.fields.<...>.nc``).
   * - AWS S3 `noaa-nos-ofs-pds <https://registry.opendata.aws/noaa-ofs/>`__ (`NOAA Open Data
       Dissemination <https://www.noaa.gov/information-technology/open-data-dissemination>`__)
     - 2024 to the present, with gaps before 2024-11-19
     - From 2024-11-19, one directory per day, ``<ofs>/netcdf/{yyyy}/{mm}/{dd}/``; the first day
       holds only its last cycle. Before that, one flat directory per month,
       ``<ofs>/netcdf/{yyyymm}/``, with a mix of file names (NYOFS 2024-07 uses the older names,
       DBOFS 2024-07 the newer ones); several of those months are partial and some are absent
       (NYOFS lacks 2024-01 to 2024-03, 2024-05 and 2024-06; DBOFS lacks 2024-02 and starts
       2024-01 on the 29th).

.. note::
   An earlier version of this page said that NCEI stopped archiving at the end of November 2023,
   that AWS began in January 2024, and that December 2023 was therefore missing. None of that
   holds: NCEI has every day of December 2023 for both NYOFS and DBOFS (124 NYOFS and 744 DBOFS
   field files) and has continued archiving through 2026, while the AWS archive is the one with
   gaps.

   The NYOFS and DBOFS fetchers read nowcast files only, so a window is a chain of analyses
   rather than forecasts. For each file they try AWS (per-day layout) first when the date is
   2024-11-19 or later, then NCEI, which covers every date; the flat AWS month directories are
   not used. A window with a file missing from both is not served by that donor, and the
   dispatcher falls back. DBOFS publishes one file per hour: file ``n001`` to ``n006`` of cycle
   ``t<cc>z`` (00, 06, 12, 18 UTC) hold hours ``cc - 5`` to ``cc``. NYOFS publishes one file per
   cycle (05, 11, 17, 23 UTC) holding the same six hours.

   Both fetchers were corrected on 2026-10-06. Before that, every NYOFS archive date failed (see
   :doc:`nyofs`), and DBOFS sent 2024 dates before 2024-11-19 to an AWS layout that does not
   hold them, took some hours from the wrong file, joined NCEI files along a ``time`` dimension
   they do not have (theirs is ``ocean_time``), and left the ROMS fill value (1e37) in land
   cells read through NCEI.

   For the AWS layout, see the `NOAA NODD OFS documentation
   <https://github.com/NOAA-Big-Data-Program/nodd-data-docs/blob/main/OFS/README.md>`_ and the
   `Registry of Open Data entry <https://registry.opendata.aws/noaa-ofs/>`_.

Common processing
-----------------

- **Hourly time axis.** Non-streaming donors are resampled to a strict hourly index by linear
  interpolation. Streaming donors deliver hourly records directly. HYCOM steps are 3 hours apart
  with occasional gaps of up to 51 hours, which this bridges with a straight line; a store does
  not yet record the longest gap it filled (:doc:`hycom`).
- **Float32 fields, full-precision coordinates.** Data variables are cast to Float32 for the
  model; ``lat``, ``lon``, ``z`` and ``z_face`` keep full precision, since Float32 longitudes near
  -74 resolve only about 8e-6 degrees.
- **Fill values.** Missing data is written as ``-9999`` (integers) or ``-9999.0`` (floats) in the
  Zarr encoding, and as NaN in memory.
- **Time in seconds.** ``z-v3`` stores encode time as seconds since their first record, which
  the model's clock uses directly.
