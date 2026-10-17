"""Environment settings, read under the forcingkit names.

forcingkit was ecodata-cache until 2026-10-05. For one release the old environment variable names
and the old default cache directory still work, each with a FutureWarning (shown by default, as
it is meant for whoever runs the service) naming the replacement. The next release drops them.
"""

import logging
import os
import warnings
from pathlib import Path

logger = logging.getLogger("forcingkit")

# New name -> old names still read, in order.
LEGACY_ENV: dict[str, tuple[str, ...]] = {
    "FORCINGKIT_CACHE_DIR": ("ECODATA_CACHE_CACHE_DIR",),
    "FORCINGKIT_MAX_WORKERS": ("ECODATA_CACHE_MAX_WORKERS",),
    # The elevation service, renamed topobathysim -> topobathykit on 2026-10-05.
    "TOPOBATHYKIT_URL": ("TOPOBATHYSIM_URL",),
}

DEFAULT_CACHE_DIR = Path("~/.cache/forcingkit")
LEGACY_CACHE_DIR = Path("~/.cache/ecodata-cache")

_warned: set[str] = set()


def _deprecated(old: str, new: str) -> None:
    if old in _warned:
        return
    _warned.add(old)
    message = f"{old} is deprecated; use {new} (forcingkit was ecodata-cache)."
    logger.warning(message)
    warnings.warn(message, FutureWarning, stacklevel=3)


def env(name: str, default: str | None = None) -> str | None:
    """The value of `name`, else of its first set legacy name (with a warning), else `default`."""
    value = os.environ.get(name)
    if value is not None:
        return value
    for old in LEGACY_ENV.get(name, ()):
        value = os.environ.get(old)
        if value is not None:
            _deprecated(old, name)
            return value
    return default


def cache_dir(*parts: str) -> str:
    """The cache root (FORCINGKIT_CACHE_DIR, default ~/.cache/forcingkit), joined with `parts`.

    While the default directory does not exist and ~/.cache/ecodata-cache does, the old directory
    is used, with a warning to move it.
    """
    configured = env("FORCINGKIT_CACHE_DIR")
    if configured is not None:
        root = Path(configured).expanduser()
    else:
        root = DEFAULT_CACHE_DIR.expanduser()
        legacy = LEGACY_CACHE_DIR.expanduser()
        if not root.exists() and legacy.exists():
            _deprecated(str(legacy), f"{root} (move the directory)")
            root = legacy
    return os.path.join(str(root), *parts)


def max_workers(default: int = 4) -> int:
    """Threads for parallel remote reads (FORCINGKIT_MAX_WORKERS)."""
    return int(env("FORCINGKIT_MAX_WORKERS", str(default)) or default)


def erddap_timeouts() -> tuple[float, float]:
    """(connect, read) timeouts in seconds for ERDDAP profile reads
    (FORCINGKIT_ERDDAP_CONNECT_TIMEOUT, default 10; FORCINGKIT_ERDDAP_READ_TIMEOUT, default 60)."""
    connect = float(env("FORCINGKIT_ERDDAP_CONNECT_TIMEOUT", "10") or 10)
    read = float(env("FORCINGKIT_ERDDAP_READ_TIMEOUT", "60") or 60)
    return connect, read


def erddap_failure_ttl() -> float:
    """Seconds a failed ERDDAP host or dataset is skipped before it is tried again
    (FORCINGKIT_ERDDAP_FAILURE_TTL, default 300; 0 turns the negative cache off)."""
    return float(env("FORCINGKIT_ERDDAP_FAILURE_TTL", "300") or 0)
