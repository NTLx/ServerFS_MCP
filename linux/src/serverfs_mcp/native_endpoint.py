"""Linux operator diagnostic data-home helpers.

The Docker deployment supplies the Bridge endpoint and lease location explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

BRIDGE_DIRECTORY = "agent-bridge"


class NativeEndpointError(Exception):
    """Redacted operator diagnostic error."""


def data_home(env=None) -> Path:
    """Preserve Linux XDG and explicit data-home configuration."""
    environ = os.environ if env is None else env
    override = environ.get("SERVERFS_DATA_HOME", "").strip()
    if override:
        return Path(override)
    xdg = environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / "serverfs"
    return Path.home() / ".local" / "share" / "serverfs"


def bridge_home(env=None) -> Path:
    return data_home(env) / BRIDGE_DIRECTORY


def derive_lock_dir(env=None) -> Path:
    return bridge_home(env) / "locks"
