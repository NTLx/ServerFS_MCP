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

#: The runtime subdirectory the Darwin Bridge socket lives in (dev_plan_v0.13.md §11 D2).
RUNTIME_DIRECTORY = "serverfs-agent-bridge-v1"
SOCKET_NAME = "bridge.sock"

__all__ = [
    "BRIDGE_DIRECTORY",
    "RUNTIME_DIRECTORY",
    "SOCKET_NAME",
    "bridge_data_home",
    "darwin_runtime_dir",
    "serverfs_data_dir",
]


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
    if sys.platform == "darwin":
        # v0.13 (§10 C2, restated per §23 — the Bridge never imports serverfs_mcp):
        # macOS keeps everything under Application Support, never the XDG defaults.
        home = environ.get("HOME", "").strip() or str(Path.home())
        return Path(home) / "Library" / "Application Support" / "ServerFS"
    xdg = environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / "serverfs"
    home = environ.get("HOME", "").strip() or str(Path.home())
    return Path(home) / ".local" / "share" / "serverfs"


def bridge_data_home(env: Mapping[str, str] | None = None) -> Path:
    """Where the Windows/macOS Bridge keeps state, locks, results and generated config."""
    return serverfs_data_dir(env) / BRIDGE_DIRECTORY


def darwin_runtime_dir() -> Path:
    """The OS-provided per-user Darwin runtime directory, validated (§11 D2 / 0G).

    Resolved with ``confstr(_CS_DARWIN_USER_TEMP_DIR)`` — never an inherited
    ``$TMPDIR`` string — and verified to be a real directory owned by the
    current UID with no group/world write bits. Importable only on Darwin.
    """
    if sys.platform != "darwin":
        raise BridgeError("BRIDGE_PLATFORM_UNSUPPORTED", "the runtime directory is Darwin-only")
    import ctypes
    import ctypes.util
    import stat as stat_module

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.confstr.restype = ctypes.c_size_t
    libc.confstr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]
    _CS_DARWIN_USER_TEMP_DIR = 65536
    buf = ctypes.create_string_buffer(1024)
    length = libc.confstr(_CS_DARWIN_USER_TEMP_DIR, buf, 1024)
    if length == 0:
        raise BridgeError("DATA_HOME_UNAVAILABLE", "the Darwin user runtime dir is unavailable")
    value = buf.value.decode("utf-8")
    if not value:
        raise BridgeError("DATA_HOME_UNAVAILABLE", "the Darwin user runtime dir is unavailable")
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise BridgeError(
            "DATA_HOME_UNAVAILABLE", "the Darwin user runtime dir is unusable"
        ) from exc
    if (
        stat_module.S_ISLNK(info.st_mode)
        or not stat_module.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise BridgeError(
            "DATA_HOME_UNAVAILABLE", "the Darwin user runtime dir failed its safety checks"
        )
    return path
