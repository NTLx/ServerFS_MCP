"""Read-only compatibility helpers for the v0.13.0/v0.13.1 macOS LaunchAgent.

v0.13.2 moved normal macOS lifecycle management to ``SMAppService`` in
:mod:`serverfs_mcp.darwin_service`.  This module intentionally retains only the
legacy plist reader needed to migrate an existing Agent Bridge configuration;
it never installs, starts, stops, or mutates a launchd job.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

AGENT_LABEL = "com.ntlx.serverfs.agent-bridge"


def launch_agent_plist_path() -> Path:
    """Return the old per-user plist path used by v0.13.0/v0.13.1."""
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def installed_bridge_config_path() -> Path | None:
    """Read the Bridge config path recorded in a legacy plist, if valid."""
    plist_path = launch_agent_plist_path()
    try:
        payload = plistlib.loads(plist_path.read_bytes())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    argv = payload.get("ProgramArguments")
    if (
        not isinstance(argv, list)
        or len(argv) < 3
        or argv[1] != "--config"
        or not isinstance(argv[2], str)
        or not argv[2]
    ):
        return None
    path = Path(argv[2]).expanduser()
    return path if path.is_absolute() else None
