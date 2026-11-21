"""W2j Windows named mutex namespace regression, isolated random fixture only.

The production Core mutex is never created or opened by this test.
"""

from __future__ import annotations

import ctypes
import secrets
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="requires Win32 Kernel")
def test_named_mutex_local_namespace_has_one_separator() -> None:
    name = "Local\\serverfs-w2j-synthetic-" + secrets.token_hex(12)
    invalid = name.replace("Local\\", "Local\\\\", 1)

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]

    assert name.count("\\") == 1
    assert invalid.count("\\") == 2
    ctypes.set_last_error(0)
    good = kernel.CreateMutexW(None, False, name)
    assert good, "one-separator Local namespace must create a test-only mutex"
    try:
        assert ctypes.get_last_error() != 183, "test fixture unexpectedly already exists"
    finally:
        kernel.CloseHandle(good)

    ctypes.set_last_error(0)
    bad = kernel.CreateMutexW(None, False, invalid)
    if bad:
        kernel.CloseHandle(bad)
        pytest.fail("Windows unexpectedly accepted a two-separator Local mutex name")
    assert ctypes.get_last_error() != 0


def test_mocked_core_mutex_uses_exactly_one_namespace_separator() -> None:
    from pathlib import Path

    from serverfs_mcp import windows_core_control

    source = Path(windows_core_control.__file__).read_text(encoding="utf-8")
    # Python source must encode precisely ONE actual namespace backslash.
    assert r'"Local\\\\serverfs-mcp-core-v1-"' not in source
    assert r'"Local\\serverfs-mcp-core-v1-"' in source
