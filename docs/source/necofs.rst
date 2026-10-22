NECOFS: Northeast Coastal Ocean Forecast System (FVCOM)
=======================================================

As of 2026-10-06. Mesh and archive facts on this page were read from the SMAST THREDDS server on
that date (the 2026-10-01 history file and the archive catalogue).

NECOFS is the recommended parent ocean from New York Harbor northward, and the only forcingkit
donor that delivers the ``z-v3`` parent store: u, v, temperature, salinity and sea surface height
on a regular longitude/latitude grid and fixed depths in metres, streamed to disk one hour at a
time.

System overview
---------------

`NECOFS <https://fvcom.smast.umassd.edu/?p=20>`__ is an operational forecast system developed by
the `University of Massachusetts Dartmouth School for Marine Science and Technology
<https://www.umassd.edu/smast/>`__ (SMAST) and the `Woods Hole Oceanographic Institution
<https://www.whoi.edu/>`__, and served from SMAST. Its operation is supported by the `Northeastern
Regional Association of Coastal Ocean Observing Systems <https://neracoos.org/>`__ (NERACOOS).
It is built on `FVCOM <https://fvcom.smast.umassd.edu/>`__, the unstructured-grid, finite-volume,
free-surface, primitive-equation coastal ocean model introduced by `Chen, Liu and Beardsley
(2003) <https://doi.org/10.1175/1520-0426(2003)020%3C0159:AUGFVT%3E2.0.CO;2>`__ and developed by
Changsheng Chen's group at SMAST with Robert C. Beardsley at WHOI. forcingkit reads its **GOM7**
configuration, which
spans the Gulf of Maine, Georges Bank, southern New England, Long Island Sound, New York Harbor
and the Mid-Atlantic Bight shelf.

Unlike NYOFS (:doc:`nyofs`), NECOFS carries temperature and salinity, so one donor supplies every
field a baroclinic child model needs.

Underlying model
~~~~~~~~~~~~~~~~

- **Hydrodynamic model**: FVCOM, 3D, free surface, hydrostatic.
- **Mesh**: unstructured triangles. Scalars (``zeta``, ``temp``, ``salinity``, ``h``) live on the
  207,081 *nodes*; velocities (``u``, ``v``, ``ww``) and depth-averaged velocities (``ua``,
  ``va``) live on the 371,290 *elements* (triangle centroids). ``nv`` lists each element's three
  nodes (one-based).
- **Vertical coordinate**: sigma, 45 layers (``siglay``, layer centres) between 46 levels
  (``siglev``), negative downward: about -0.011 for the top layer to -0.989 for the bottom one.
  ``siglay`` is stored per node.
- **Time**: seconds since 1858-11-17 00:00 (Modified Julian Day origin), with the integer pair
  ``Itime``/``Itime2`` alongside; forcingkit drops the pair and decodes ``time``.

Mesh resolution
~~~~~~~~~~~~~~~

Element size, as the equivalent edge length :math:`\sqrt{2A}` of each triangle of area *A*:

.. list-table::
   :header-rows: 1
   :widths: 46 14 14 13 13

   * - Region (element centroids inside)
     - Elements
     - 10th pct.
     - Median
     - 90th pct.
   * - Whole mesh, [-77.96, 31.84, -56.85, 46.15]
     - 371,290
     - 197 m
     - 473 m
     - 2832 m
   * - New York Harbor, [-74.3, 40.4, -73.8, 40.9]
     - 7,325
     - 133 m
     - 254 m
     - 797 m
   * - Long Island Sound, [-73.8, 40.8, -72.0, 41.4]
     - 8,563
     - 164 m
     - 608 m
     - 1579 m

The fetcher declares 200 m and the domain [-77.0, 35.0, -65.0, 46.0] for ranking (see
:doc:`fetchers`); the mesh itself reaches further offshore.

Where the data lives
--------------------

All access is OPeNDAP from ``http://www.smast.umassd.edu:8080/thredds/dodsC/models/fvcom/NECOFS/``.

.. list-table::
   :header-rows: 1
   :widths: 20 30 50

   * - Product
     - Path
     - Contents
   * - Daily history
     - ``Archive/necofs_history/NECOFS_GOM7_{yyyy}_{mm}_{dd}.nc``
     - 24 hourly records. The file stamped day *D* holds D-1 01:00 to D 00:00 (UTC), so the record
       at midnight is in the file of the same date and every other hour of *D* is in the file of
       *D* + 1.
   * - Rolling forecast
     - ``Forecasts/NECOFS_GOM7_FORECAST.nc``
     - The current run; 193 time records on 2026-10-06.

The history archive held 577 files on 2026-10-06, from 2025-01-01 to 2026-10-05. **66 days are
missing**, so a run that needs one of them cannot be built from NECOFS:

- 2025-01-12 to 2025-01-25 (14 days, the longest gap);
- 2025: 03-08, 04-01, 04-13, 05-22, 05-24 to 05-26, 06-06, 06-08, 06-14, 06-23, 07-27, 07-29,
  08-31, 09-05 to 09-06, 09-15, 09-20, 10-15 to 10-17, 10-26, 12-11 to 12-12, 12-14;
- 2026: 01-03, 02-16, 02-25, 03-08, 03-24, 05-16 to 05-17, 05-28, 06-06 to 06-09, 06-17,
  07-02 to 07-03, 07-13 to 07-15, 08-25.

Older history is not on the public server. Because each file also holds the first hour of its own
date, a missing file *D* removes hours 01:00 to 23:00 of *D* - 1 and the 00:00 record of *D*.

How forcingkit reads it
-----------------------

``forcingkit.fetchers.necofs.iter_parent(start_date, duration_hours, bbox, pad_cells=3,
vertical_spacing_m=2.0)`` is a generator; the dispatcher writes what it yields straight to disk.

1. **File choice.** For each hour the fetcher picks the history file that holds it and probes
   it (an HTTP request for its ``.dds``, 5 s timeout). If the probe fails, it opens the rolling
   forecast instead, which serves recent hours and fails for older ones.
2. **Target grid.** A regular 0.002 degree longitude/latitude grid (about 170 m east-west and
   220 m north-south at 41 N) covering the request box plus ``pad_cells`` cells on every side, so
   the parent brackets the child.
3. **Horizontal interpolation, built once per request.** Node fields are interpolated linearly
   within the mesh element that contains each target point (``MeshBarycentric``), as FVCOM itself
   interpolates within an element. A target inside no element (land, or outside the mesh) is NaN.
   This matters along the coast: a plain Delaunay triangulation of the nodes would also span the
   land between them (a peninsula, an island, the shore between two estuaries) and fill it with
   values blended across it. Element fields (``u``, ``v``) are interpolated linearly over a
   triangulation of the element centroids, then masked to the same wet points.
4. **Vertical coordinate.** Each hour, every column's layer depths are computed from the free
   surface: :math:`z = \sigma (h + \zeta) + \zeta`. Fields are interpolated linearly onto fixed
   levels every ``vertical_spacing_m`` metres, from the surface to the deepest bed in the box.
   Above the shallowest layer centre the top value is used, below the deepest one the bottom
   value; levels below the local bed are NaN.
5. **Records.** ``duration_hours`` hourly records from ``start_date``, computed in parallel
   (``FORCINGKIT_MAX_WORKERS`` threads, default 4) in batches of that size, so memory holds at
   most that many hours at once.

Any unreachable file, missing hour or failed interpolation raises. The writer then deletes the
partial store, so a store either has every requested hour or does not exist.

Output: the ``z-v3`` parent store
---------------------------------

.. list-table::
   :header-rows: 1
   :widths: 14 22 18 46

   * - Name
     - Dimensions
     - Units
     - Meaning
   * - ``u``, ``v``
     - (time, z, lat, lon)
     - m s-1
     - Eastward and northward velocity.
   * - ``temp``
     - (time, z, lat, lon)
     - degree_Celsius
     - Sea water temperature.
   * - ``salt``
     - (time, z, lat, lon)
     - 1e-3
     - Practical salinity.
   * - ``zeta``
     - (time, lat, lon)
     - m
     - Sea surface height (FVCOM's ``zeta``).
   * - ``h``
     - (lat, lon)
     - m
     - Sea floor depth, positive down.
   * - ``mask``
     - (lat, lon)
     - 1
     - 1 where NECOFS has ocean, 0 on land or outside the mesh.
   * - ``lon``, ``lat``
     - 1D
     - degrees
     - Regular axes, full (Float64) precision.
   * - ``z``, ``z_face``
     - 1D
     - m, positive up
     - Level centres and faces, bottom to top, full precision.

Records are Float32, one Zarr chunk per hour. ``time`` is encoded as seconds since the first
record. Store attributes record ``schema = "z-v3"``, ``complete = true``, ``records``,
``requested_bbox``, ``pad_cells``, ``vertical_spacing_m``, ``duration_hours``, ``sponge_cells``
and ``source``. Before publishing, the writer checks that the record count equals
``duration_hours`` and that the records are hourly and contiguous. Records are written to a
partial store beside the final path, which is renamed into place only when complete, so a reader
never sees a store still being written.

API usage
---------

A box in central Long Island Sound, outside the NYOFS and DBOFS domains, so NECOFS ranks first:

.. code-block:: bash

   curl -s -X POST http://localhost:9598/api/v1/obc \
     -H 'Content-Type: application/json' \
     -d '{"bbox": {"min_lon": -73.20, "min_lat": 40.95, "max_lon": -73.00, "max_lat": 41.10},
          "start_date": "2026-09-26T00:00:00Z", "duration_hours": 24,
          "pad_cells": 3, "vertical_spacing_m": 2.0}'

Fields of the request that shape a NECOFS store:

.. list-table::
   :header-rows: 1
   :widths: 24 12 64

   * - Field
     - Default
     - Effect
   * - ``bbox``
     - required
     - The child domain; the store covers it plus ``pad_cells`` on every side.
   * - ``start_date``, ``duration_hours``
     - required
     - First record and number of hourly records.
   * - ``pad_cells``
     - 3
     - Donor cells (0.002 degree each) added beyond the box on every side.
   * - ``vertical_spacing_m``
     - 2.0
     - Spacing of the fixed z levels.
   * - ``allow_donor_fallback``
     - true
     - When false, a NECOFS failure is an error rather than a hand-off to the next donor.
   * - ``cache_bust``
     - false
     - Rebuild the store even if a complete one exists.

From Python, either stream records yourself or assemble them in memory:

.. code-block:: python

   from forcingkit.fetchers.necofs import iter_parent, fetch_necofs_boundary_conditions

   bbox = [-73.20, 40.95, -73.00, 41.10]
   for item in iter_parent("2026-09-26T00:00:00Z", 24, bbox):
       if item[0] == "static":
           static = item[1]              # lon, lat, z, z_face, h, mask, attrs
       else:
           _, t, record = item           # record: u, v, temp, salt, zeta as numpy arrays

   ds = fetch_necofs_boundary_conditions("2026-09-26T00:00:00Z", 24, bbox)

Full-depth subset on the native mesh
------------------------------------

For work that needs FVCOM's own geometry rather than a regular grid (3D rendering of the sigma
prisms, particle tracking with FVCOM's own interpolation, comparisons element by element),
``volume(bbox, start, end)`` returns every sigma layer for the elements whose centroids lie in
the box, with no regridding and no vertical conversion. ``read_volume(bbox, start_time, hours,
dap_url=None)`` does the same for one file.

.. code-block:: python

   from forcingkit.fetchers.necofs import volume

   bbox = [-72.22, 41.10, -71.75, 41.395]  # Fishers Island and The Race, with a margin
   vol = volume(bbox, "2026-04-02T00Z", "2026-04-04T00Z")  # 49 hourly records

.. list-table::
   :header-rows: 1
   :widths: 22 26 52

   * - Name
     - Dimensions
     - Meaning
   * - ``u``, ``v``, ``ww``
     - (time, siglay, element)
     - Velocities at element layer centres, m s-1. ``ww`` is the vertical velocity in z, which
       FVCOM derives from the sigma-coordinate velocity omega; omega itself is not archived.
   * - ``temp``, ``salinity``
     - (time, siglay, node)
     - At node layer centres.
   * - ``zeta``; ``h``
     - (time, node); (node)
     - Surface elevation; depth below the geoid, positive down.
   * - ``siglay``, ``siglev``
     - (siglay, node), (siglev, node)
     - Sigma at layer centres and interfaces, surface first. The depth of a layer centre is
       :math:`\sigma (h + \zeta) + \zeta`.
   * - ``x``, ``y``; ``xc``, ``yc``
     - (node); (element)
     - The model's own Cartesian coordinates in metres, NAD83 / Maine West (EPSG:26984).
   * - ``node_lon``, ``node_lat``; ``lonc``, ``latc``
     - (node); (element)
     - Degrees.
   * - ``triangles``
     - (element, vertex)
     - Zero-based indices into the returned nodes.
   * - ``neighbours``
     - (element, vertex)
     - The element across the edge opposite each vertex (FVCOM's ``NBE`` order), as an index into
       the returned elements; ``NO_NEIGHBOUR`` (-1) on the mesh boundary, ``OUTSIDE_SUBSET`` (-2)
       where the neighbour exists but was not selected.
   * - ``on_boundary``
     - (node)
     - The node lies on the mesh boundary (coast or open boundary).
   * - ``element_id``, ``node_id``
     - (element); (node)
     - Indices in the full mesh.

The archive does not carry FVCOM's edge-neighbour table, so ``neighbours`` and ``on_boundary`` are
rebuilt from ``nv`` over the **whole** mesh (``forcingkit.mesh_frames.element_neighbours``): on
the subset alone, every element along the subset's edge would look like a coastal element.

Every requested hour must be found, and every archive file must select the same elements;
otherwise ``volume`` raises. Results are cached as Zarr under
``$FORCINGKIT_CACHE_DIR/necofs/volume/``, keyed by the schema (``volume-v1``), the box and the
window. Hyperslabs are fetched with ``FORCINGKIT_MAX_WORKERS`` threads. Even so, the 48 hours
above (2,831 elements, 1,829 nodes, 45 layers, about 110 MB) took about five minutes from SMAST
on 2026-10-22, almost all of it server round trips, and 0.1 s from the cache.

Wetting and drying
~~~~~~~~~~~~~~~~~~

GOM7 runs with FVCOM's wet/dry treatment, although no file attribute says so and the wet/dry
masks are not archived. The data show it: the total depth :math:`h + \zeta` never falls below
0.200 m, thousands of nodes sit exactly at that floor at low water (5,641 in the record of
2026-04-02 12:00 UTC), and 99 % of the elements whose three nodes are at the floor carry exactly
zero ``u``, ``v`` and ``ww``. Treat an element as dry when all three of its nodes are at the floor.

Notes and limits
----------------

- **Archive gaps.** See the list above. A request that touches a missing day fails; with
  fallback allowed, the dispatcher then tries :doc:`hycom`, which covers every NECOFS-era date
  but at 1/12 degree and in the legacy output layout.
- **Probe timeout.** If SMAST is slow to answer the 5 s probe, the fetcher opens the rolling
  forecast for that hour and, for anything older than the forecast, fails. Retrying usually
  succeeds.
- **Plain HTTP.** The SMAST server is reached over plain HTTP on port 8080.
- **Element fields across narrow land.** ``u`` and ``v`` are interpolated over a triangulation
  of the element centroids, which is not bounded by the coastline, and then masked to the wet
  points. A wet point beside a narrow strip of land (a breakwater, a thin spit) can therefore take
  part of its velocity from centroids on the far side. Scalars do not have this problem, since
  they are interpolated within the mesh elements themselves.

Tests
-----

``tests/unit/test_necofs_parent.py`` covers the z levels, the sigma-to-z interpolation (linear
profiles reproduced, surface on top, NaN below the bed and on land, never zero), the choice of
history file for each hour, the barycentric weights against SciPy's ``LinearNDInterpolator``, the
mesh interpolation leaving land between elements NaN, the masking of element fields, the schema
tag, and the passing of donor options through the dispatcher.

.. code-block:: bash

   uv run pytest tests/unit/test_necofs_parent.py -v

``tests/unit/test_necofs_volume.py`` covers the full-depth subset on a synthetic FVCOM file:
every value lands on its own element, node, layer and hour; neighbours are mutual, and the
subset's edge is never mistaken for the coast; boundary nodes; windows assembled across
archive files (the midnight record in its own day's file); a missing hour raising; and the
cache. ``tests/integration/test_necofs_volume_live.py`` reads three hours across a file
boundary from SMAST.

References
----------

Citations
~~~~~~~~~

- Chen, C., H. Liu and R. C. Beardsley (2003). An unstructured grid, finite-volume,
  three-dimensional, primitive equations ocean model: application to coastal ocean and
  estuaries. *Journal of Atmospheric and Oceanic Technology*, 20(1), 159-186.
  `doi:10.1175/1520-0426(2003)020<0159:AUGFVT>2.0.CO;2
  <https://doi.org/10.1175/1520-0426(2003)020%3C0159:AUGFVT%3E2.0.CO;2>`__
- Beardsley, R. C., C. Chen and Q. Xu (2013). Coastal flooding in Scituate (MA): A FVCOM study
  of the 27 December 2010 nor'easter. *Journal of Geophysical Research: Oceans*, 118(11),
  6030-6045. `doi:10.1002/2013JC008862 <https://doi.org/10.1002/2013JC008862>`__ (an application
  of the NECOFS system to a coastal storm)

Institutions and data services
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- `UMass Dartmouth SMAST <https://www.umassd.edu/smast/>`__: `FVCOM
  <https://fvcom.smast.umassd.edu/>`__, `NECOFS <https://fvcom.smast.umassd.edu/?p=20>`__,
  `THREDDS catalogue
  <http://www.smast.umassd.edu:8080/thredds/catalog/models/fvcom/NECOFS/catalog.html>`__
- `Woods Hole Oceanographic Institution <https://www.whoi.edu/>`__
- `NERACOOS <https://neracoos.org/>`__
