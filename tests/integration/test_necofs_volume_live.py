"""Live smoke test: three hours of NECOFS GOM7 at every sigma layer on the native mesh, across an
archive-file boundary.

Reads the SMAST THREDDS server over OPeNDAP. Run with
`uv run python -m pytest tests/integration/test_necofs_volume_live.py`.
"""

import numpy as np
import pandas as pd
import pytest

from forcingkit.fetchers import necofs

pytestmark = pytest.mark.integration

BOX = (-72.12, 41.19, -72.03, 41.26)  # The Race, eastern Long Island Sound


def test_necofs_volume_across_files():
    day = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3)).normalize()
    vol = necofs.volume(
        BOX, day - pd.Timedelta(hours=1), day + pd.Timedelta(hours=1), cache=False
    )
    assert len(vol.time) == 3 and len(vol.attrs["urls"]) == 2
    assert vol.sizes["siglay"] == 45 and vol.sizes["siglev"] == 46
    assert vol.sizes["element"] > 50
    for name in ("u", "v", "ww", "temp", "salinity", "zeta"):
        assert np.isfinite(vol[name].values).all(), name
    # Bed friction: the bottom layer is slower than the surface layer on average.
    speed = np.hypot(vol["u"].values, vol["v"].values)
    assert speed[:, 0].mean() > speed[:, -1].mean()
    # Triangles reference returned nodes; neighbours are -1, -2 or an element index.
    assert vol["triangles"].values.max() < vol.sizes["node"]
    nb = vol["neighbours"].values
    assert ((nb >= -2) & (nb < vol.sizes["element"])).all()
    # x, y are the model's own Cartesian metres (EPSG:26984), not degrees.
    assert np.ptp(vol["x"].values) > 1000.0
