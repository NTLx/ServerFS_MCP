"""W2 installed-environment import check under Windows Python isolated mode.

No Task Scheduler, Bridge, Core HTTP listener or Connector process starts.
The interpreter's site-packages must resolve every v0.25 entrypoint without
PYTHONPATH or the current directory in sys.path.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

_MODULES = (
    "serverfs_mcp.windows_bridge_service",
    "serverfs_mcp.windows_core_service",
    "serverfs_mcp.windows_connector_service",
    "serverfs_mcp.windows_core_listener",
)


@pytest.mark.parametrize("module", _MODULES)
def test_native_python_isolated_mode_imports_component_without_running_it(module: str) -> None:
    # Explicit -I: an editable-source checkout and ambient PYTHONPATH must
    # not accidentally supply a missing wheel during real Task Scheduler use.
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import importlib,sys; importlib.import_module(sys.argv[1])",
        module,
    ]
    result = subprocess.run(command, env=env, capture_output=True, timeout=12, check=False)
    assert result.returncode == 0, (
        f"isolated-mode component import failed: {module}; exit={result.returncode}"
    )
