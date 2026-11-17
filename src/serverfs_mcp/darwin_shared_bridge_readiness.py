"""Read-only readiness probe for an explicitly configured Shared Bridge v2."""

from __future__ import annotations

import asyncio
import math
import os
import stat
from pathlib import Path

from .core_bridge_client_v2 import CoreBridgeClientV2, CoreBridgeClientV2Error


async def probe_shared_v2_bridge(
    client: CoreBridgeClientV2,
    expected_socket_path: Path,
    *,
    timeout_seconds: float = 5.0,
    require_private_parent: bool = False,
) -> bool:
    """Return true only when the expected private socket answers authenticated v2 ping.

    A successful ping proves only that the Bridge accepted this signature key
    for ``bridge.ping``. It does not establish authorization for other methods
    or Core ownership/policy for the caller's Unix UID.
    """
    if (
        not isinstance(expected_socket_path, Path)
        or not expected_socket_path.is_absolute()
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 30
    ):
        return False
    try:
        path = expected_socket_path
        for parent in path.parents:
            if stat.S_ISLNK(parent.lstat().st_mode):
                return False
        info = path.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return False
        if require_private_parent:
            parent_info = path.parent.lstat()
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != os.getuid()
                or stat.S_IMODE(parent_info.st_mode) != 0o700
            ):
                return False
        if not isinstance(client, CoreBridgeClientV2):
            return False
        if client._socket_path != path:
            return False
        async with asyncio.timeout(float(timeout_seconds)):
            return await client.call("bridge.ping", {}) == {"status": "ok"}
    except (OSError, CoreBridgeClientV2Error, TimeoutError, ValueError, TypeError):
        # Keep path details, remote errors, and credential-bearing exceptions private.
        return False
