"""Test-only isolated Windows Core HTTP worker; NOT a product service.

The caller supplies a disposable read-only root and OS-assigned loopback port.
A random per-test bearer is delivered only in this worker's child environment,
never on the command line, in the repository, or in a process log.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server, run_streamable_http
from serverfs_mcp.workdirs import Workdir, WorkdirRegistry


def main() -> int:
    if sys.platform != "win32" or len(sys.argv) != 3:
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir() or not root.is_absolute():
        return 2
    try:
        port = int(sys.argv[2])
    except ValueError:
        return 2
    if not 1 <= port <= 65535:
        return 2
    token = os.environ.pop("SERVERFS_TEMP_CORE_TOKEN", "")
    if len(token) < 32:
        return 2
    registry = WorkdirRegistry(
        [Workdir(alias="Repo", root=root, description=root.parent.name, read_only=True)]
    )
    settings = Settings(
        log_level="WARNING",
        http_host="127.0.0.1",
        http_port=port,
        http_path="/mcp",
        http_api_key=token,
        http_allowed_hosts=(f"127.0.0.1:{port}",),
        http_allowed_origins=(f"http://127.0.0.1:{port}",),
    )
    mcp = create_server(settings, registry)
    run_streamable_http(mcp, settings, registry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
