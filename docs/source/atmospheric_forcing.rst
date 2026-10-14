Atmospheric Forcing Datasets
============================

As of 2026-10-04. Which atmospheric datasets can force a coastal ocean model through this service
or through `NumericalEarth <https://github.com/NumericalEarth/NumericalEarth.jl>`__, how they
compare, and which to choose.

An ocean model's surface fluxes need, at every hour of the run: 10 m wind (eastward and
northward), 2 m air temperature and specific humidity, surface pressure, precipitation, and
downwelling shortwave and longwave radiation. The bulk formulae in NumericalEarth turn these into
momentum, heat and freshwater fluxes; any dataset missing one of them cannot drive the model on its
own.

Summary
-------

.. list-table::
   :header-rows: 1
   :widths: 13 14 12 12 14 14 21

   * - Dataset
     - Type
     - Grid
     - Output step
     - Coverage
     - Latency
     - Path to a model
   * - **HRRR** (`NOAA GSL <https://gsl.noaa.gov/>`__, `NCEP <https://www.nco.ncep.noaa.gov/>`__)
     - Hourly-cycling forecast, 3 km convection-allowing, radar assimilation
     - 3 km, Lambert conformal over CONUS
     - 1 h (15 min for some fields)
     - CONUS; archive on AWS from 2014-07-30
     - About an hour after each cycle
     - **forcingkit** ``/api/v1/atmosphere`` (schema ``hrrr-atm-v1``); the default
   * - **ERA5** (`ECMWF <https://www.ecmwf.int/>`__, `Copernicus C3S
       <https://climate.copernicus.eu/>`__)
     - Global reanalysis (4D-Var)
     - 0.25 degrees (about 31 km)
     - 1 h
     - 1940 to present
     - ERA5T about 5 days; final ERA5 2 to 3 months
     - **NumericalEarth** ``ERA5PrescribedAtmosphere``
   * - **RRFS v1** (`NOAA GSL <https://gsl.noaa.gov/rrfs/>`__, NCEP)
     - Hourly-cycling forecast, FV3 limited-area, 3 km
     - 3 km, North America
     - 1 h
     - Operational from 2026-10-06 (12 UTC cycle)
     - Hourly cycles to 18 h; to 84 h every 6 h
     - Not integrated; replaces NAM, SREF, HREF and HiresW, while HRRR stays in operations
   * - **NAM CONUS Nest** (NOAA NCEP)
     - Forecast, 3 km
     - 3 km, CONUS
     - 1 h
     - Retired 2026-10-06 in favour of RRFS
     - Four cycles a day, to 60 h
     - Not integrated; do not adopt
   * - **GFS** (`NOAA NCEP
       <https://www.emc.ncep.noaa.gov/emc/pages/numerical_forecast_systems/gfs.php>`__)
     - Global forecast, FV3
     - 0.25 degrees
     - 1 h to 120 h, then 3 h
     - Global; NODD archive on AWS
     - Four cycles a day, to 384 h
     - Not integrated; the natural extension past HRRR's 48 h in forecast mode
   * - **ECMWF IFS** `open data <https://www.ecmwf.int/en/forecasts/datasets/open-data>`__
     - Global forecast
     - 0.25 degrees
     - 3 h, then 6 h
     - Global; real time
     - Four cycles a day, to 15 days (00 and 12 UTC)
     - Not integrated; CC-BY-4.0 since 2025-10-01
   * - **JRA55-do** (JMA, `MRI <https://www.mri-jma.go.jp/index_en.html>`__)
     - Reanalysis adjusted for ocean-sea-ice models
     - About 0.5 degrees
     - 3 h
     - 1958-01-01 to 2024-02-01, final version 1.6.0
     - Discontinued (JRA-55 ended January 2024; successor JRA-3Q)
     - **NumericalEarth** ``JRA55PrescribedAtmosphere`` (its catalogue ends 2019-12-31)
   * - **ECCO v4** (`ECCO Consortium <https://ecco-group.org/>`__, NASA JPL)
     - Ocean state estimate's adjusted forcing
     - About 1 degree
     - Monthly
     - Multi-decadal
     - Not real time
     - **NumericalEarth** ``ECCOPrescribedAtmosphere``; climate scale only

Datasets in use
---------------

HRRR (default)
~~~~~~~~~~~~~~

*Provenance.* NOAA's `High-Resolution Rapid Refresh <https://rapidrefresh.noaa.gov/hrrr/>`__,
version 4, developed by the NOAA Global Systems Laboratory and run by NCEP: a 3 km
convection-allowing model that assimilates radar every 15 minutes and starts a new forecast every
hour (`Dowell et al., 2022 <https://doi.org/10.1175/WAF-D-21-0151.1>`__; `James et al., 2022
<https://doi.org/10.1175/WAF-D-21-0130.1>`__). Distributed through the `NOAA Open Data
Dissemination <https://www.noaa.gov/information-technology/open-data-dissemination>`__ programme
in the `noaa-hrrr-bdp-pds <https://registry.opendata.aws/noaa-hrrr-pds/>`__ bucket on AWS
(us-east-1), anonymous, from 2014-07-30 to the present. NOAA open data: free to use; NOAA asks
for attribution and that modified products not be presented as NOAA's.

*How this service builds it* (``fetchers/hrrr_atmosphere.py``). Hour ``t`` is the one-hour forecast
(``wrfsfcf01``) of the cycle that started at ``t - 1 h``. Every record is therefore a short forecast
from the latest analysis, and cycles are chained, so a run of any length is covered without
reaching into the long-range end of any single forecast. Eight GRIB2 messages are read per hour by
byte range from the ``.idx`` sidecar:

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - Message
     - Store variable
     - Treatment
   * - ``UGRD``, ``VGRD`` at 10 m
     - ``u10``, ``v10`` (m/s)
     - Rotated from the Lambert grid axes to east and north (HRRR winds are grid-relative), angle
       ``sin(38.5 deg) * (lon + 97.5 deg)``, checked against ``pyproj``
   * - ``TMP`` at 2 m
     - ``t2m`` (K)
     -
   * - ``SPFH`` at 2 m
     - ``q2m`` (kg/kg)
     - Used directly; no dewpoint conversion
   * - ``PRES`` at the surface
     - ``sp`` (Pa)
     - Surface, not sea-level, pressure
   * - ``APCP`` 0-1 h accumulation
     - ``prate`` (kg m-2 s-1)
     - Mean of the accumulations ending at ``t`` and ``t + 1 h``, divided by 3600 s: a rate
       centred on ``t``
   * - ``DSWRF``, ``DLWRF`` at the surface
     - ``dswrf``, ``dlwrf`` (W m-2)
     - Instantaneous at ``t``; hourly sampling resolves the diurnal cycle but aliases short cloud
       transients. Hourly means exist only for shortwave, in the sub-hourly files

Fields are interpolated (barycentric, weights built once per request) to a regular 0.03 degree
longitude-latitude grid covering the bbox plus 0.25 degrees, which is the grid type NumericalEarth's
atmosphere regridder accepts. Records run from one hour before the run start to one hour after its
end; a missing message or hour raises, and the store is published only when complete. Building
one hour takes about 6.5 s on a warm connection, so a 168 h window takes about 19 minutes.

*Strengths.* Resolves the land-sea contrast, sea breezes and frontal timing at the scale of
coastal domains from about 15 to 85 km across. Hourly radiation and precipitation. No credentials.

*Limits.* CONUS only. A forecast product, not a reanalysis: it carries forecast-model biases and is
not homogeneous across HRRR versions (v4 since December 2020). Radiation is instantaneous.

HRRR forecasts at points
~~~~~~~~~~~~~~~~~~~~~~~~

``forcingkit.fetchers.hrrr_points.fetch_points(cycles, leads, points, fields)`` returns HRRR forecasts
at caller-supplied points for any cycles and lead hours, one row per (issue time, valid time, lead,
point, field, value). Keeping both times makes the record bitemporal: a forecast can be compared
with what was later observed, by how far ahead it was made. Fields are 10 m wind (rotated to east
and north), gust, 2 m temperature and dew point, sea-level pressure and visibility, in the units
HRRR publishes. Each point takes an inverse-distance-squared mean of up to four HRRR water cells
within 6 km, so a coastal point never takes a land cell's temperature or wind.

Leads a cycle does not carry are skipped (HRRR v4 runs to 48 h at 00, 06, 12 and 18 UTC and to
18 h otherwise); a missing field in a published file raises. Three leads of seven fields at four
points took 28 s on a warm connection, so one cycle's leads 1 to 18 take about three minutes.

.. code-block:: python

   from forcingkit.fetchers.hrrr_points import fetch_points

   df = fetch_points(["2026-10-14T08:00"], [1, 6, 18], {"buoy": (-73.58, 40.96)})

ERA5 (fallback)
~~~~~~~~~~~~~~~

*Provenance.* `ECMWF <https://www.ecmwf.int/>`__'s fifth-generation global reanalysis for the
`Copernicus Climate Change Service <https://climate.copernicus.eu/>`__ (`Hersbach et al., 2020
<https://doi.org/10.1002/qj.3803>`__): 0.25 degree grid (about 31 km), hourly, 1940 to the present.
ERA5T, the initial release,
appears about five days behind real time and is overwritten by the final ERA5 two to three months
later. Requires a free `Copernicus Climate Data Store <https://cds.climate.copernicus.eu/>`__
account (credentials in ``~/.cdsapirc``);
Copernicus licence, attribution required.

*How it is used.* A model reads it through NumericalEarth's ``ERA5PrescribedAtmosphere`` and
``ERA5PrescribedRadiation``, for example over the bbox padded by 0.5 degrees, with linear time
indexing and one hour of padding past the end. Accumulated fields (precipitation, radiation) are
hour-ending means that NumericalEarth places at the centre of their hour. NumericalEarth 0.8.1's
catalogue stops at 2025-12-31; later dates need the catalogue extended until upstream rolls it.

*Strengths.* Homogeneous, global, long, assimilates far more observations than any forecast; the
standard against which forcing biases are judged.

*Limits.* At 31 km a 15 km coastal domain spans one or two ERA5 cells, so the forcing is nearly
uniform and smears the coast: in the first hours of 2026-04-02 over such a domain off New Jersey,
the ERA5 box (which includes New Jersey land) was about 3 K warmer at 2 m and had about half HRRR's
wind speed. Latency rules out
anything closer than five days to the present.

Choosing
--------

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Need
     - Dataset
   * - Hindcast of a CONUS coastal domain from 2014-07-30 on
     - HRRR (default)
   * - Hindcast before 2014-07-30, outside CONUS, or a reanalysis-grade comparison run
     - ERA5
   * - Forecast to 48 h
     - HRRR forecast mode (planned): one cycle's f01 to f48 from the 00, 06, 12 or 18 UTC
       cycle
   * - Forecast beyond 48 h
     - HRRR to 48 h, then GFS or ECMWF IFS open data (not integrated); RRFS to 84 h once it is
       established in operations
   * - Multi-decade or climate-scale forcing
     - ERA5, or JRA55-do (`Tsujino et al., 2018 <https://doi.org/10.1016/j.ocemod.2018.07.002>`__)
       through NumericalEarth (to 2019 in its catalogue)

References
----------

Citations
~~~~~~~~~

- Dowell, D. C., C. R. Alexander, E. P. James, et al. (2022). The High-Resolution Rapid Refresh
  (HRRR): An hourly updating convection-allowing forecast model. Part I: Motivation and system
  description. *Weather and Forecasting*, 37(8), 1371-1395.
  `doi:10.1175/WAF-D-21-0151.1 <https://doi.org/10.1175/WAF-D-21-0151.1>`__
- James, E. P., C. R. Alexander, D. C. Dowell, et al. (2022). The High-Resolution Rapid Refresh
  (HRRR): An hourly updating convection-allowing forecast model. Part II: Forecast performance.
  *Weather and Forecasting*, 37(8), 1397-1417.
  `doi:10.1175/WAF-D-21-0130.1 <https://doi.org/10.1175/WAF-D-21-0130.1>`__
- Hersbach, H., B. Bell, P. Berrisford, et al. (2020). The ERA5 global reanalysis. *Quarterly
  Journal of the Royal Meteorological Society*, 146(730), 1999-2049.
  `doi:10.1002/qj.3803 <https://doi.org/10.1002/qj.3803>`__
- Tsujino, H., S. Urakawa, H. Nakano, et al. (2018). JRA-55 based surface dataset for driving
  ocean-sea-ice models (JRA55-do). *Ocean Modelling*, 130, 79-139.
  `doi:10.1016/j.ocemod.2018.07.002 <https://doi.org/10.1016/j.ocemod.2018.07.002>`__

Links
~~~~~

- NOAA HRRR on AWS: https://registry.opendata.aws/noaa-hrrr-pds/
- NOAA RRFS: https://gsl.noaa.gov/rrfs/ ; operational date and retirements:
  https://gribstream.com/blog/noaa-rrfs-refs-operational-august-2026
- ERA5T latency:
  https://climate.copernicus.eu/key-update-climate-dataset-brings-data-five-days-behind-real-time
- ECMWF open data: https://www.ecmwf.int/en/forecasts/datasets/open-data
- JRA55-do: https://climate.mri-jma.go.jp/pub/ocean/JRA55-do/
