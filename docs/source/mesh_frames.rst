Mesh frames: native-mesh image frames
=====================================

``forcingkit.mesh_frames`` reads model output on the model's own unstructured mesh (no
regridding) and ``forcingkit.render`` draws it as a sequence of PNG frames: one flat colour per
triangle, with an optional mesh-edge overlay. NECOFS FVCOM GOM7 surface currents are the first
registered model.

Data contract
-------------

``MeshFrames`` holds ``times`` (UTC), ``node_lon`` and ``node_lat``, ``tri`` (``n_tri`` x 3,
zero-based node indices) and ``u``, ``v`` (``[t, n_tri]``, one value per element, NaN where an
element has no value). Any triangle-mesh model fits it; an adapter is one function registered with
``@register_adapter("name")`` that takes ``(bbox, times)`` and returns a ``MeshFrames``. The
``forcingkit.mesh_frames`` module docstring shows the template.

Projection
----------

Pixels are plain longitude/latitude, defined exactly by the caller's box ``(W, S, E, N)`` and image
size ``(w, h)``: pixel column ``i`` spans longitudes ``W + [i, i+1) * (E - W) / w`` and row ``j``
spans latitudes ``N - [j, j+1) * (N - S) / h``. A pixel takes the colour of the element containing
its centre, so frames drawn with the same box and size line up pixel for pixel with any other
renderer that uses the same convention. Outside the mesh (and wherever an element is NaN) is
transparent, or the background colour when one is given.

The mesh is rasterised once per (mesh, box, size) into an element-index image and the edges once
into an overlay; both are cached, and each frame is a colour lookup per element and per pixel. On a
synthetic mesh of 52 000 elements, 45 frames at 1600 x 900 render in about 0.6 s plus 0.5 s for the
index image; writing the PNGs takes about 4.5 s.

Colour
------

``color`` is any vectorised function from speed (m/s, shape ``(n,)``) to uint8 RGB or RGBA
(``(n, 3)`` or ``(n, 4)``). Use a fixed scale so a colour means the same speed in every frame:

- ``default_color``: viridis, linear over 0 to 1.5 m/s;
- ``named_color(name, vmin, vmax, log=False, floor=None)``: a built-in ramp (``viridis``,
  ``greys``) over a chosen range;
- ``lut_color(rows, log=False, floor=None)``: a lookup table of ``(value, r, g, b[, a])`` rows.
  Values below the first row, or below ``floor``, take the first colour; between rows each channel
  is interpolated linearly in the value, or in log(value) when ``log`` is true; values above the
  last row take the last colour; NaN is transparent. ``load_lut_csv(path)`` reads such a table
  from a ``value,r,g,b[,a]`` CSV;
- ``packaged_color("currents_lis")``: the surface-current table shipped with the package
  (``forcingkit/ramps/currents_lis.csv``), 0.02 to 3.1 m/s on a log axis, pale blue through
  yellow to dark green with CIELAB lightness falling monotonically. Its CSV header records the
  knots and their source;
- ``packaged_color("currents_lis_dark")``: its twin for a dark basemap, on the same speeds, from
  midnight blue through steel blue to pale seafoam with lightness rising monotonically, so the
  two swap by theme alone.

Python
------

.. code-block:: python

   from forcingkit.mesh_frames import mesh_frames
   from forcingkit.render import lut_color, render_frames, write_frames

   bbox = (-73.0, 40.95, -72.6, 41.15)  # central Long Island Sound
   frames = mesh_frames("necofs", bbox, "2026-10-15T22:00Z", "2026-10-16T02:00Z", stride_h=1)
   color = lut_color([(0.05, 230, 240, 250), (0.5, 120, 160, 200), (2.0, 20, 40, 90)], log=True)
   images = render_frames(
       frames, bbox, (1200, 800), color,
       edges=((0, 0, 0, 40), 1), background=(242, 243, 240, 255),
   )
   write_frames(images, "frames/", times=frames.times)

NECOFS hours are read from the SMAST daily history file that holds each one (falling back to the
rolling forecast), so a window may cross several archive days.

Command line
------------

.. code-block:: bash

   uv run python -m forcingkit.render --model necofs \
     --bbox -73.0 40.95 -72.6 41.15 \
     --start 2026-10-15T22:00Z --end 2026-10-16T02:00Z --stride-h 1 \
     --size 1200x800 --ramp viridis --vmax 1.5 \
     --edges 0,0,0,40 --background 242,243,240 --out frames/

``--ramp`` takes a built-in name, a packaged table (``currents_lis``, ``currents_lis_dark``) or a path to a
``value,r,g,b[,a]`` CSV (with ``--log`` and ``--floor``). The output directory receives
``frame_0000.png``, ``frame_0001.png``, ... and ``frames.json``, which lists the frame times, the box
and the size.
