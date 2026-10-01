"""Import-safety gate for the v0.10 Phase A platform boundary.

The MCP product layer and the backend contract modules must be importable
on a platform whose ServerFS kernel modules cannot load at all:
``serverfs_mcp.fdio`` computes Linux-only ``os.O_DIRECTORY``/``os.O_NOFOLLOW``
at import time, and ``serverfs_mcp.agent_leases`` is Unix-only (it imports
fcntl). A clean subprocess force-blocks those module names and then imports
the product layers — strictly stronger than a source-text scan, because it
fails on ANY transitive ServerFS import, wherever it hides.

fcntl itself is deliberately not blocked: the pinned mcp SDK imports it on
POSIX behind its own platform guard, and that import is outside this
project's contract. What must hold is that no ServerFS product-layer module
drags in a kernel that needs it — which is exactly what blocking the two
kernel modules proves. On a real Windows host (where fcntl cannot exist)
this same test is the full Phase B import-safety evidence.
"""

from __future__ import annotations

import subprocess
import sys

_SCRIPT = """
import sys

class _Blocker:
    BLOCKED = frozenset({"serverfs_mcp.fdio", "serverfs_mcp.agent_leases"})

    def find_spec(self, name, path=None, target=None):
        if name in self.BLOCKED:
            raise ImportError(f"platform kernel not importable here: {name}")
        return None

sys.meta_path.insert(0, _Blocker())

import serverfs_mcp.backends
import serverfs_mcp.binary_payload
import serverfs_mcp.concurrency
import serverfs_mcp.errors
import serverfs_mcp.tools
import serverfs_mcp.main

# the contract itself must still be usable as a specification
from serverfs_mcp.backends import BackendError, FilesystemBackend, WorkdirSession, get_backend

assert issubclass(BackendError, Exception)
assert WorkdirSession is not None and FilesystemBackend is not None
print("IMPORT_SAFE")
"""


def test_product_layers_import_without_fdio_or_agent_leases() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"blocked import failed:\n{result.stderr}"
    assert "IMPORT_SAFE" in result.stdout


def test_linux_kernel_imports_stay_outside_the_contract() -> None:
    """backends.py has no module-level (non-lazy) import of a platform kernel."""
    from pathlib import Path

    from serverfs_mcp import backends

    text = Path(backends.__file__).read_text(encoding="utf-8")
    module_level = [line for line in text.splitlines() if line.startswith(("from ", "import "))]
    joined = "\n".join(module_level)
    for banned in ("fdio", "linux_backend", "fcntl", "mutations", "search", "agent_leases"):
        assert banned not in joined, f"backends.py imports {banned!r} at module level"
