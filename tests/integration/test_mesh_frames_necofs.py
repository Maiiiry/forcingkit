"""Live smoke test: two hours of NECOFS GOM7 surface currents on the native mesh, rendered.

Reads the SMAST THREDDS server over OPeNDAP. Run with
`uv run python -m pytest tests/integration/test_mesh_frames_necofs.py`.
"""

import numpy as np
import pandas as pd
import pytest

from forcingkit import render
from forcingkit.mesh_frames import mesh_frames

pytestmark = pytest.mark.integration

BOX = (-73.0, 40.95, -72.6, 41.15)  # central Long Island Sound


def test_necofs_mesh_frames_render(tmp_path):
    day = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3)).normalize()
    frames = mesh_frames(
        "necofs", BOX, day + pd.Timedelta(hours=24), day + pd.Timedelta(hours=25)
    )
    assert len(frames) == 2  # crosses an archive-file boundary
    assert frames.tri.shape[0] > 100
    assert np.isfinite(frames.speed()).mean() > 0.9
    paths = render.write_frames(
        render.render_frames(frames, BOX, (400, 200)), tmp_path, times=frames.times
    )
    assert len(paths) == 2
    img = np.asarray(render.Image.open(paths[0]))
    assert img.shape == (200, 400, 4) and (img[..., 3] > 0).mean() > 0.2
