"""Platform classification for the Agent Bridge suite (v0.11 Phase A, extended in Phase B).

The Bridge core above the §3 seams is platform-neutral and its tests run on
every platform. Below those seams sit the platform mechanisms. On Linux that is
the Unix-socket server, SO_PEERCRED, the flock writer lease and UID/mode
private-state authorization; Phase B added the Windows twins for local IPC, peer
identity and private state (Named Pipe + measured client SID, owner SID + NTFS
DACL), so a test that constructs one of those mechanisms now runs on both
platforms, and the POSIX-side tests below that remain classification rather than
a limitation.

Two Windows seams still fail closed with BRIDGE_PLATFORM_UNSUPPORTED because
their twins are later phases: the flock writer lease is Phase C and process
containment is Phase D. A Windows-mechanism test is skipped on Linux for the
same reason it was skipped on Windows before Phase B: it is the positive
contract of one platform's implementation.
"""

from __future__ import annotations

import sys

import pytest

LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"

__all__ = [
    "LINUX",
    "WINDOWS",
    "linux_only",
    "require_linux_kernel",
    "require_windows_kernel",
    "windows_only",
]


def require_linux_kernel(reason: str) -> None:
    if not LINUX:
        pytest.skip(f"Linux Bridge contract: {reason}", allow_module_level=True)


def require_windows_kernel(reason: str) -> None:
    if not WINDOWS:
        pytest.skip(f"Windows Bridge contract: {reason}", allow_module_level=True)


def linux_only(reason: str):
    return pytest.mark.skipif(not LINUX, reason=f"Linux Bridge contract: {reason}")


def windows_only(reason: str):
    return pytest.mark.skipif(not WINDOWS, reason=f"Windows Bridge contract: {reason}")
