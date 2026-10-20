"""Flat-shaded image frames of per-element values on a triangle mesh.

Projection is plain longitude/latitude to pixels, fixed by the caller's bbox (W, S, E, N) and image
size (w, h): pixel column i spans longitudes W + [i, i+1) * (E - W) / w and row j spans latitudes
N - [j, j+1) * (N - S) / h, so any other renderer given the same bbox and size lines up with these
frames pixel for pixel. Each element is one flat colour; nothing is interpolated.

For speed the mesh is rasterised once per (mesh, bbox, size) into an element-index image (int32,
-1 outside the mesh), and mesh edges once into their own overlay; both are cached. A frame is then
a colour per element and one table lookup per pixel.

Command line (writes numbered PNGs and a frames.json index)::

    python -m forcingkit.render --model necofs --bbox -73.0 40.95 -72.6 41.15 \\
        --start 2026-10-01T00:00Z --end 2026-10-01T12:00Z --size 1600x900 --out frames/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Iterator, Sequence

import numpy as np
from PIL import Image, ImageDraw

from forcingkit.mesh_frames import MeshFrames, available_models, mesh_frames

logger = logging.getLogger(__name__)

ColorFn = Callable[[np.ndarray], np.ndarray]
RGBA = tuple[int, int, int, int]

# Named ramps as (fraction, r, g, b) knots on [0, 1], scaled to [vmin, vmax] by `named_color`.
RAMPS: dict[str, list[tuple[float, int, int, int]]] = {
    # Five samples of the viridis map (perceptually uniform, colour-vision-deficiency safe).
    "viridis": [
        (0.00, 68, 1, 84),
        (0.25, 59, 82, 139),
        (0.50, 33, 145, 140),
        (0.75, 94, 201, 98),
        (1.00, 253, 231, 37),
    ],
    "greys": [(0.0, 240, 240, 240), (1.0, 30, 30, 30)],
}
DEFAULT_RAMP = "viridis"
DEFAULT_VMAX = 1.5  # m/s


# Colour functions ------------------------------------------------------------------------------


def lut_color(
    rows: Sequence[Sequence[float]],
    log: bool = False,
    floor: float | None = None,
) -> ColorFn:
    """A colour function from a lookup table of (value, r, g, b) or (value, r, g, b, a) rows.

    The returned function maps an array of values (n,) to uint8 RGBA (n, 4):

    - below the first knot, or below `floor` when given, the first colour;
    - between knots, linear interpolation of each channel in the value, or in log(value) when
      `log` is True (all knot values must then be positive);
    - above the last knot, the last colour;
    - NaN, fully transparent (0, 0, 0, 0).

    Rows are sorted by value; alpha defaults to 255.
    """
    table = np.array([list(r) + [255] * (5 - len(r)) for r in rows], dtype=np.float64)
    if table.ndim != 2 or table.shape[1] != 5 or len(table) < 1:
        raise ValueError("LUT rows must be (value, r, g, b[, a])")
    table = table[np.argsort(table[:, 0], kind="stable")]
    knots = table[:, 0]
    if log and knots[0] <= 0:
        raise ValueError("log interpolation needs every knot value > 0")
    xp = np.log(knots) if log else knots
    channels = table[:, 1:]
    first = np.rint(channels[0]).astype(np.uint8)

    def color(values: np.ndarray) -> np.ndarray:
        v = np.asarray(values, dtype=np.float64)
        nan = ~np.isfinite(v)
        vv = np.where(nan, knots[0], v)
        x = np.log(np.maximum(vv, knots[0])) if log else vv
        out = np.empty(v.shape + (4,), dtype=np.uint8)
        for c in range(4):
            out[..., c] = np.rint(np.interp(x, xp, channels[:, c])).astype(np.uint8)
        if floor is not None:
            out[vv < floor] = first
        out[nan] = 0
        return out

    return color


def load_lut_csv(path: str | Path) -> list[list[float]]:
    """Rows of a `value,r,g,b[,a]` CSV. Blank lines, `#` comments and a non-numeric header are skipped."""
    rows: list[list[float]] = []
    with open(path, newline="") as fh:
        for rec in csv.reader(fh):
            if not rec or rec[0].strip().startswith("#"):
                continue
            try:
                rows.append([float(x) for x in rec if x.strip() != ""])
            except ValueError:
                if rows:
                    raise
                continue  # header
    if not rows:
        raise ValueError(f"{path} holds no LUT rows")
    return rows


def named_color(
    name: str = DEFAULT_RAMP,
    vmin: float = 0.0,
    vmax: float = DEFAULT_VMAX,
    log: bool = False,
    floor: float | None = None,
) -> ColorFn:
    """A built-in ramp (see RAMPS) stretched over [vmin, vmax]."""
    if name not in RAMPS:
        raise KeyError(f"Unknown ramp {name!r}; built-in: {sorted(RAMPS)}")
    if log:
        if vmin <= 0:
            raise ValueError("log ramps need vmin > 0")
        lo, hi = np.log(vmin), np.log(vmax)
        rows = [
            (float(np.exp(lo + f * (hi - lo))), r, g, b) for f, r, g, b in RAMPS[name]
        ]
    else:
        rows = [(vmin + f * (vmax - vmin), r, g, b) for f, r, g, b in RAMPS[name]]
    return lut_color(rows, log=log, floor=floor)


# Lookup tables shipped as package data (forcingkit/ramps/<name>.csv), each with the
# interpolation it was designed for. Their CSV headers record how they were built.
PACKAGED_RAMPS: dict[str, dict] = {
    # Surface currents, 0.02 to 3.1 m/s on a log axis; lightness falls monotonically.
    "currents_lis": {"log": True, "floor": 0.02},
    # Its twin for a dark basemap: the same speeds, lightness rising monotonically.
    "currents_lis_dark": {"log": True, "floor": 0.02},
    # Multi-hue alternatives on the same speeds, after cmocean "deep" (light backgrounds; lightness
    # falling) and "haline" (dark; rising), for a field shown alone with its legend.
    "currents_lis_deep": {"log": True, "floor": 0.02},
    "currents_lis_haline": {"log": True, "floor": 0.02},
}


def packaged_ramp_rows(name: str) -> list[list[float]]:
    """Rows of a packaged lookup table (see PACKAGED_RAMPS)."""
    from importlib.resources import as_file, files

    if name not in PACKAGED_RAMPS:
        raise KeyError(
            f"Unknown packaged ramp {name!r}; available: {sorted(PACKAGED_RAMPS)}"
        )
    with as_file(files("forcingkit") / "ramps" / f"{name}.csv") as path:
        return load_lut_csv(path)


def packaged_color(name: str) -> ColorFn:
    """Colour function for a packaged lookup table, with its own log axis and floor."""
    return lut_color(packaged_ramp_rows(name), **PACKAGED_RAMPS[name])


def default_color(values: np.ndarray) -> np.ndarray:
    """The default ramp: viridis, linear over 0 to 1.5 m/s."""
    return _DEFAULT(values)


_DEFAULT = named_color()


# Projection and rasterisation -----------------------------------------------------------------


def lonlat_to_px(
    lon: np.ndarray, lat: np.ndarray, bbox: Sequence[float], size: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """Continuous pixel coordinates (x right, y down) with (0, 0) at the top-left corner of the
    top-left pixel and (w, h) at the bottom-right corner of the image. Pixel (i, j) has its centre
    at (i + 0.5, j + 0.5)."""
    w_, s_, e_, n_ = (float(b) for b in bbox)
    w, h = size
    x = (np.asarray(lon, dtype=np.float64) - w_) * (w / (e_ - w_))
    y = (n_ - np.asarray(lat, dtype=np.float64)) * (h / (n_ - s_))
    return x, y


_CACHE_MAX = 8
_INDEX_CACHE: OrderedDict[tuple, np.ndarray] = OrderedDict()
_EDGE_CACHE: OrderedDict[tuple, Image.Image] = OrderedDict()


def _mesh_key(frames: MeshFrames, bbox, size, *extra) -> tuple:
    h = hashlib.blake2b(digest_size=16)
    for a in (frames.node_lon, frames.node_lat, frames.tri):
        h.update(np.ascontiguousarray(a).tobytes())
    return (h.hexdigest(), tuple(float(b) for b in bbox), tuple(size)) + extra


def _cached(cache: OrderedDict, key: tuple, build: Callable):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    value = build()
    cache[key] = value
    while len(cache) > _CACHE_MAX:
        cache.popitem(last=False)
    return value


def element_index_image(
    frames: MeshFrames, bbox: Sequence[float], size: tuple[int, int]
) -> np.ndarray:
    """(h, w) int32: the element containing each pixel centre, -1 outside the mesh. Cached per
    (mesh, bbox, size).

    Sampling at pixel centres makes the result exact for the projection: a pixel belongs to an
    element when its centre lies inside that triangle (a centre on a shared edge goes to one of the
    two). The point location uses matplotlib's trapezoid-map triangle finder, which needs a valid
    mesh (no overlapping elements), as model meshes are.
    """

    def build() -> np.ndarray:
        from matplotlib.tri import Triangulation

        w, h = size
        idx = np.full((h, w), -1, dtype=np.int32)
        if frames.tri.shape[0] == 0:
            return idx
        x, y = lonlat_to_px(frames.node_lon, frames.node_lat, bbox, size)
        used = np.unique(frames.tri)
        # Only pixel centres inside the mesh's own extent need a lookup.
        c0 = max(0, int(np.floor(x[used].min() - 0.5)))
        c1 = min(w, int(np.ceil(x[used].max() + 0.5)))
        r0 = max(0, int(np.floor(y[used].min() - 0.5)))
        r1 = min(h, int(np.ceil(y[used].max() + 0.5)))
        if c1 <= c0 or r1 <= r0:
            return idx
        finder = Triangulation(x, y, frames.tri).get_trifinder()
        px, py = np.meshgrid(np.arange(c0, c1) + 0.5, np.arange(r0, r1) + 0.5)
        found = np.asarray(finder(px.ravel(), py.ravel()), dtype=np.int32)
        idx[r0:r1, c0:c1] = found.reshape(px.shape)
        return idx

    return _cached(_INDEX_CACHE, _mesh_key(frames, bbox, size), build)


def edge_overlay(
    frames: MeshFrames,
    bbox: Sequence[float],
    size: tuple[int, int],
    color: RGBA,
    width: int = 1,
) -> Image.Image:
    """Transparent RGBA image with every mesh edge drawn once at `color` and `width` pixels. Cached.

    Lines are drawn with Pillow, which places line ends on whole pixels, so an edge can sit up to
    half a pixel from its exact position; the fill (`element_index_image`) is exact.
    """

    def build() -> Image.Image:
        w, h = size
        e = np.concatenate(
            [frames.tri[:, [0, 1]], frames.tri[:, [1, 2]], frames.tri[:, [2, 0]]]
        )
        e = np.unique(np.sort(e, axis=1), axis=0)
        x, y = lonlat_to_px(frames.node_lon, frames.node_lat, bbox, size)
        pts = np.stack([x - 0.5, y - 0.5], axis=-1)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        a, b = pts[e[:, 0]], pts[e[:, 1]]
        keep = ~(
            (np.maximum(a[:, 0], b[:, 0]) < -width)
            | (np.minimum(a[:, 0], b[:, 0]) > w + width)
            | (np.maximum(a[:, 1], b[:, 1]) < -width)
            | (np.minimum(a[:, 1], b[:, 1]) > h + width)
        )
        fill = tuple(int(c) for c in color)
        for p, q in zip(a[keep].tolist(), b[keep].tolist()):
            draw.line([tuple(p), tuple(q)], fill=fill, width=int(width))
        return img

    key = _mesh_key(frames, bbox, size, "edges", tuple(color), int(width))
    return _cached(_EDGE_CACHE, key, build)


def clear_cache() -> None:
    """Drop cached element-index images and edge overlays."""
    _INDEX_CACHE.clear()
    _EDGE_CACHE.clear()


# Rendering -------------------------------------------------------------------------------------


def render_frames(
    frames: MeshFrames,
    bbox: Sequence[float],
    size: tuple[int, int],
    color: ColorFn = default_color,
    edges: tuple[RGBA, int] | None = None,
    background: RGBA | None = None,
    antialias: int = 1,
) -> Iterator[Image.Image]:
    """One RGBA image per time in `frames`, colouring each element by its speed.

    bbox: (W, S, E, N) in degrees; size: (width, height) in pixels. Together they fix the
    projection (see the module docstring).
    color: vectorised speed (n,) in m/s -> uint8 (n, 3) or (n, 4); RGB gets alpha 255. Pass a
        fixed scale (for example `lut_color` or `named_color`) so colours mean the same in every
        frame.
    edges: None, or ((r, g, b, a), width_px) to draw mesh edges over the fill.
    background: None for transparent outside the mesh (and for NaN elements), or an RGBA colour.
    antialias: 1 (default) draws hard pixel edges; n > 1 renders at n times the size and
        box-filters down.
    """
    w, h = (int(s) for s in size)
    k = max(1, int(antialias))
    big = (w * k, h * k)
    idx = element_index_image(frames, bbox, big)
    overlay = None
    if edges is not None:
        overlay = edge_overlay(frames, bbox, big, edges[0], int(edges[1]) * k)
    bg = np.array(
        background if background is not None else (0, 0, 0, 0), dtype=np.uint8
    )
    n_tri = frames.tri.shape[0]
    speed = frames.speed()
    for t in range(len(frames)):
        rgba = np.asarray(color(speed[t]))
        if rgba.shape[:1] != (n_tri,) or rgba.shape[-1] not in (3, 4):
            raise ValueError(f"color returned {rgba.shape}, expected ({n_tri}, 3|4)")
        lut = np.empty((n_tri + 1, 4), dtype=np.uint8)
        lut[:n_tri, : rgba.shape[-1]] = rgba
        if rgba.shape[-1] == 3:
            lut[:n_tri, 3] = 255
        lut[:n_tri][~np.isfinite(speed[t])] = bg
        lut[n_tri] = bg  # idx -1 selects this row
        img = Image.fromarray(lut[idx], "RGBA")
        if overlay is not None:
            img.alpha_composite(overlay)
        if k > 1:
            img = img.resize((w, h), Image.Resampling.BOX)
        yield img


def write_frames(
    images: Iterator[Image.Image],
    out_dir: str | Path,
    prefix: str = "frame",
    times: Sequence | None = None,
    meta: dict | None = None,
) -> list[Path]:
    """Save images as `<prefix>_0000.png`, `<prefix>_0001.png`, ... in `out_dir`, plus a
    `frames.json` index (file names, ISO times when given, and `meta`). Returns the PNG paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, img in enumerate(images):
        p = out / f"{prefix}_{i:04d}.png"
        img.save(p)
        paths.append(p)
    index: dict = {"frames": [p.name for p in paths]}
    if times is not None:
        index["times"] = [t.isoformat() for t in times][: len(paths)]
    if meta:
        index.update(meta)
    (out / "frames.json").write_text(json.dumps(index, indent=1))
    return paths


# Command line ----------------------------------------------------------------------------------


def _rgba(text: str) -> RGBA:
    parts = [int(p) for p in text.replace(" ", "").split(",")]
    if len(parts) == 3:
        parts.append(255)
    if len(parts) != 4 or not all(0 <= p <= 255 for p in parts):
        raise argparse.ArgumentTypeError(f"expected r,g,b[,a] in 0..255, got {text!r}")
    return parts[0], parts[1], parts[2], parts[3]


def _size(text: str) -> tuple[int, int]:
    try:
        w, h = (int(p) for p in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected WIDTHxHEIGHT, got {text!r}"
        ) from None
    return w, h


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m forcingkit.render",
        description="Render model output on its native mesh as numbered PNG frames.",
    )
    p.add_argument("--model", default="necofs", choices=available_models())
    p.add_argument(
        "--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N")
    )
    p.add_argument(
        "--start", required=True, help="ISO time, UTC unless an offset is given"
    )
    p.add_argument("--end", required=True, help="ISO time, inclusive")
    p.add_argument("--stride-h", type=int, default=1)
    p.add_argument(
        "--size", type=_size, default=(1600, 900), help="WIDTHxHEIGHT pixels"
    )
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--prefix", default="frame")
    p.add_argument(
        "--ramp",
        default=DEFAULT_RAMP,
        help=(
            f"built-in ramp ({', '.join(sorted(RAMPS))}, stretched over --vmin to --vmax), "
            f"packaged table ({', '.join(sorted(PACKAGED_RAMPS))}, fixed scale), "
            "or a CSV of value,r,g,b[,a] rows"
        ),
    )
    p.add_argument(
        "--vmin",
        type=float,
        default=0.0,
        help="built-in ramps: value of the first colour",
    )
    p.add_argument(
        "--vmax",
        type=float,
        default=DEFAULT_VMAX,
        help="built-in ramps: value of the last colour",
    )
    p.add_argument("--log", action="store_true", help="interpolate in log(speed)")
    p.add_argument(
        "--floor",
        type=float,
        default=None,
        help="speeds below this take the first colour",
    )
    p.add_argument(
        "--edges", type=_rgba, default=None, metavar="R,G,B[,A]", help="draw mesh edges"
    )
    p.add_argument("--edge-width", type=int, default=1)
    p.add_argument("--background", type=_rgba, default=None, metavar="R,G,B[,A]")
    p.add_argument(
        "--antialias", type=int, default=1, help="supersampling factor (1 = off)"
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    if args.ramp in PACKAGED_RAMPS:
        color = packaged_color(args.ramp)
    elif args.ramp in RAMPS:
        color = named_color(
            args.ramp, args.vmin, args.vmax, log=args.log, floor=args.floor
        )
    else:
        color = lut_color(load_lut_csv(args.ramp), log=args.log, floor=args.floor)
    frames = mesh_frames(args.model, args.bbox, args.start, args.end, args.stride_h)
    edges = (args.edges, args.edge_width) if args.edges is not None else None
    images = render_frames(
        frames, args.bbox, args.size, color, edges, args.background, args.antialias
    )
    paths = write_frames(
        images,
        args.out,
        args.prefix,
        frames.times,
        meta={"bbox": list(args.bbox), "size": list(args.size), "model": args.model},
    )
    logger.info(f"wrote {len(paths)} frames to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
