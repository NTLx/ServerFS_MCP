"""The Bridge data home (§6.2, §22).

The v0.10 Windows deployment already defines where ServerFS keeps its own state:

```text
%LOCALAPPDATA%\\ServerFS          (override: SERVERFS_DATA_HOME)
```

and the Agent Bridge lives one directory below it, in ``agent-bridge``. The Bridge is a
separate distribution and must not import ``serverfs_mcp`` to reuse that one-liner (§23), so
the contract is restated here and locked by a parity test against the MCP-side helper.

Linux keeps the XDG contract and its existing explicit configuration paths; this module only
supplies defaults, and the Phase E deployment continues to pass its own paths explicitly.

Nothing here falls back to the working directory, a temp directory or a guessed home: if the
platform location cannot be determined, the caller fails closed (§22).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from .errors import BridgeError

BRIDGE_DIRECTORY = "agent-bridge"

__all__ = ["BRIDGE_DIRECTORY", "bridge_data_home", "serverfs_data_dir"]


def serverfs_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """The user-owned ServerFS data directory, matching the v0.10 MCP contract."""
    environ = os.environ if env is None else env
    override = environ.get("SERVERFS_DATA_HOME", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = environ.get("LOCALAPPDATA", "").strip()
        if not base:
            raise BridgeError(
                "DATA_HOME_UNAVAILABLE",
                "LOCALAPPDATA is not set; configure SERVERFS_DATA_HOME",
            )
        return Path(base) / "ServerFS"
    xdg = environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / "serverfs"
    home = environ.get("HOME", "").strip() or str(Path.home())
    return Path(home) / ".local" / "share" / "serverfs"


def bridge_data_home(env: Mapping[str, str] | None = None) -> Path:
    """Where the Windows Bridge keeps state, locks, results and generated config."""
    return serverfs_data_dir(env) / BRIDGE_DIRECTORY
