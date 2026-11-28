"""Test platform classification (v0.11 Phase A).

The root suite contains three kinds of test and they must not be conflated:

- portable: they drive the MCP product surface, which dispatches through
  ``serverfs_mcp.backends.get_backend()`` and therefore runs on every platform;
- Linux-kernel contract: they exercise ``fdio``/``mutations``/``filesystem``/``search``,
  ``flock``-based leases or POSIX permission bits directly. The Windows native kernel has its own
  coverage in ``test_windows_backend``/``test_native_windows``/``test_windows_path_acceptance``, so
  skipping these on Windows loses no Windows behaviour;
- Windows-kernel contract: the native-kernel files listed above.

``require_linux_kernel`` is called at module import time, before the Linux-only imports, so a
platform that cannot import the FD layer collects instead of erroring. ``IS_ROOT`` replaces
module-scope ``os.geteuid()`` evaluation, which does not exist off POSIX and used to break
collection on its own.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

__all__ = [
    "LINUX",
    "WINDOWS",
    "IS_ROOT",
    "require_linux_kernel",
    "linux_only",
    "windows_difference",
    "settle_file_time",
]


def require_linux_kernel(reason: str) -> None:
    if not LINUX:
        pytest.skip(f"Linux kernel contract: {reason}", allow_module_level=True)


def linux_only(reason: str):
    """Decorator for one test or class that asserts a Linux/POSIX mechanism.

    ``reason`` names the mechanism, so a reader can tell why the assertion cannot
    hold on another kernel and what covers the same product intent there.
    """
    return pytest.mark.skipif(not LINUX, reason=f"Linux kernel contract: {reason}")


def windows_difference(reason: str):
    """Decorator for a measured cross-platform behaviour difference in the kernel.

    It records the difference instead of hiding it: the test still runs on Windows
    and reports as expected-failure, and the reason states what differs.
    """
    return pytest.mark.xfail(condition=WINDOWS, reason=reason, strict=False)


def settle_file_time() -> None:
    """Wait past one file-timestamp step before asserting a revision changed.

    Windows records a file's times from the sampled system clock, so two
    mutations that fall inside one step share an identical stat tuple and an
    identical revision token (measured on WorkPC: 17 of 20 same-size rewrites in
    a tight loop kept the same revision; consecutive recorded times were 0.4 ms
    to 15 ms apart). Linux stamps in nanoseconds, so the same sequence always
    differs. A real caller is separated by far more than one step, so tests that
    assert "the revision changed because the object changed" wait past a step
    instead of depending on how fast the test process happens to run.

    Re-evaluated after Phase C and kept: under contract decision B this is not a workaround for a
    defect but the shape of the promise — the Windows revision detects an observable metadata
    change, and a same-tick same-size rewrite is the documented blind window
    (dev_plan_v0.11 §15). The accepted boundary itself is asserted in
    ``test_revision.TestWindowsAcceptedRevisionBoundary``, not hidden behind this wait.
    """
    time.sleep(0.05)
