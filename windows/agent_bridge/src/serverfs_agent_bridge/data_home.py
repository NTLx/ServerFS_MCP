"""Windows Bridge data-home defaults; preserve explicit SERVERFS_DATA_HOME override."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .errors import BridgeError

BRIDGE_DIRECTORY = "agent-bridge"

__all__ = [
    "BRIDGE_DIRECTORY",
    "bridge_data_home",
    "serverfs_data_dir",
]


def serverfs_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """The user-owned ServerFS data directory, matching the v0.10 MCP contract."""
    environ = os.environ if env is None else env
    override = environ.get("SERVERFS_DATA_HOME", "").strip()
    if override:
        return Path(override)
    base = environ.get("LOCALAPPDATA", "").strip()
    if not base:
        raise BridgeError(
            "DATA_HOME_UNAVAILABLE",
            "LOCALAPPDATA is not set; configure SERVERFS_DATA_HOME",
        )
    return Path(base) / "ServerFS"


def bridge_data_home(env: Mapping[str, str] | None = None) -> Path:
    """The Bridge-owned state, locks, results and generated configuration root."""
    return serverfs_data_dir(env) / BRIDGE_DIRECTORY
