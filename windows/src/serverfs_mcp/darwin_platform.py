"""The v0.13 macOS runtime gate (dev_plan_v0.13.md §2.1).

v0.13 supports exactly: native arm64 processes on Apple M-series Macs
running macOS 27.x. Everything else fails closed — never silently execute
on an unvalidated platform. The gate is measured, not assumed from labels:

- ``sys.platform == "darwin"``
- ``platform.machine() == "arm64"``
- macOS major version == 27 (patch releases inside 27 remain allowed)
- NOT Rosetta-translated (``sysctl proc_translated`` == 0)

A gate result that cannot be measured (unknown translation state, empty
version) is unsupported, by design.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
import sys
from dataclasses import dataclass

SUPPORTED_MACOS_MAJOR = 27
SUPPORTED_MACHINE = "arm64"

REASON_NOT_DARWIN = "not a Darwin platform"
REASON_NOT_ARM64 = "processor is not native arm64"
REASON_WRONG_MACOS = "macOS version is not 27.x"
REASON_ROSETTA = "process is running under Rosetta translation"
REASON_UNMEASURABLE = "platform could not be verified (fail-closed)"

# The agent-facing code is fixed by dev_plan_v0.13.md §2.1.
UNSUPPORTED_CODE = "NATIVE_PLATFORM_UNSUPPORTED"


@dataclass(frozen=True)
class DarwinPlatformStatus:
    supported: bool
    machine: str
    macos_version: str
    rosetta: bool | None  # None = unmeasurable
    reason: str  # "" when supported


def _rosetta_translated() -> bool | None:
    """sysctl.proc_translated: 1 = Rosetta, 0 = native, None = unknown."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:
        return None
    libc.sysctlbyname.restype = ctypes.c_int
    libc.sysctlbyname.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    value = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    rc = libc.sysctlbyname(
        b"sysctl.proc_translated", ctypes.byref(value), ctypes.byref(size), None, 0
    )
    if rc != 0:
        return None
    return value.value == 1


def _macos_version() -> str:
    return platform.mac_ver()[0] or ""


def darwin_platform_status() -> DarwinPlatformStatus:
    """Measure this process against the v0.13 platform contract."""
    machine = platform.machine()
    macos = _macos_version()
    if sys.platform != "darwin":
        return DarwinPlatformStatus(False, machine, macos, None, REASON_NOT_DARWIN)
    rosetta = _rosetta_translated()
    major = int(macos.split(".")[0]) if macos else None
    if machine != SUPPORTED_MACHINE:
        return DarwinPlatformStatus(False, machine, macos, rosetta, REASON_NOT_ARM64)
    if major != SUPPORTED_MACOS_MAJOR:
        return DarwinPlatformStatus(False, machine, macos, rosetta, REASON_WRONG_MACOS)
    if rosetta is None:
        return DarwinPlatformStatus(False, machine, macos, None, REASON_UNMEASURABLE)
    if rosetta:
        return DarwinPlatformStatus(False, machine, macos, True, REASON_ROSETTA)
    return DarwinPlatformStatus(True, machine, macos, False, "")


def ensure_supported_darwin() -> None:
    """Raise RuntimeError with the fixed code prefix when unsupported.

    The caller (backends.get_backend / CLI / doctor) maps this onto the
    coded-error surface it owns; the reason text is operator-facing and
    carries no secrets.
    """
    status = darwin_platform_status()
    if not status.supported:
        raise RuntimeError(f"{UNSUPPORTED_CODE}: {status.reason}")
