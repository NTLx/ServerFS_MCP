"""Platform classification for the Agent Bridge suite (v0.11 Phase A).

The Bridge core above the §3 seams is platform-neutral and its tests run on
every platform. Below those seams sit the Linux mechanisms — the Unix-socket
server, SO_PEERCRED, the flock writer lease and UID/mode private-state
authorization — which fail closed with BRIDGE_PLATFORM_UNSUPPORTED in a Windows
Bridge process until v0.11 Phases B-D provide the Windows twins. A test that
constructs one of those mechanisms is therefore a Linux-contract test; that is a
classification, not a permanent limitation, and Phases B-D are expected to turn
these back into portable tests.
"""

from __future__ import annotations

import sys

import pytest

LINUX = sys.platform.startswith("linux")

__all__ = ["LINUX", "linux_only", "require_linux_kernel"]


def require_linux_kernel(reason: str) -> None:
    if not LINUX:
        pytest.skip(f"Linux Bridge contract: {reason}", allow_module_level=True)


def linux_only(reason: str):
    return pytest.mark.skipif(not LINUX, reason=f"Linux Bridge contract: {reason}")
