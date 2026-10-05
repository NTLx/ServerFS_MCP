"""Windows writer-lease backend: an exclusive ``LockFileEx`` over the Bridge's own artifact.

Phase 0A froze the shape (§5.5, ``docs/windows-phase-0a-lockfileex-2026-10-04.md``): both sides open
the existing artifact with ``GENERIC_READ`` and ``OPEN_EXISTING`` and take a non-blocking exclusive
full-range lock, and the object is verified on that same handle because an attribute-only handle
cannot lock at all. The Bridge creates the artifacts at startup (§5.2 — the mutation reader may
never create one); acquisition here never creates.

``LockFileEx`` is OS-enforced, so this module is legal only on lease artifacts, never on a workdir
file or on any path an Agent runtime may touch (§5.5 item 7).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BridgeError
from .lease_identity import describe

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x8000_0000
FILE_SHARE_READ = 0x0000_0001
FILE_SHARE_WRITE = 0x0000_0002
FILE_SHARE_DELETE = 0x0000_0004
LEASE_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x0000_0080
FILE_ATTRIBUTE_DIRECTORY = 0x0000_0010
FILE_ATTRIBUTE_REPARSE_POINT = 0x0000_0400

LOCKFILE_EXCLUSIVE_LOCK = 0x0000_0002
LOCKFILE_FAIL_IMMEDIATELY = 0x0000_0001
_FULL_RANGE = 0xFFFF_FFFF

ERROR_LOCK_VIOLATION = 33

_FILE_ATTRIBUTE_TAG_INFO = 9  # FILE_INFO_BY_HANDLE_CLASS is zero-based

_KERNEL32.CreateFileW.restype = wintypes.HANDLE
_KERNEL32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
_KERNEL32.LockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_KERNEL32.UnlockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_KERNEL32.GetFileInformationByHandleEx.restype = wintypes.BOOL
_KERNEL32.GetFileInformationByHandleEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]

_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def _zeroed_overlapped() -> _Overlapped:
    """A fresh zero-offset OVERLAPPED for one call.

    ``LockFileEx`` reads ``Overlapped.Offset`` for the range start even on a handle opened without
    ``FILE_FLAG_OVERLAPPED``, and a NULL pointer faults on this OS build — measured while
    implementing §5.5, where every call with ``None`` raised an access violation at offset ``0x10``
    and the same call with a real structure succeeded. A new object per call also keeps two threads
    from sharing the structure's internal fields.
    """
    return _Overlapped()


@dataclass
class WindowsWorkdirLease:
    """A held lease. Releasing is explicit but not load-bearing: the OS reclaims the lock when the
    handle closes, including after ``TerminateProcess``, which is what makes a crashed Bridge
    recoverable through its guard instead of deadlocked."""

    handle: int
    path: Path
    lease_id: str = ""
    _closed: bool = field(default=False, repr=False)

    def release(self) -> None:
        if self._closed:
            return
        self._closed = True
        _KERNEL32.UnlockFileEx(
            self.handle, 0, _FULL_RANGE, _FULL_RANGE, ctypes.byref(_zeroed_overlapped())
        )
        _KERNEL32.CloseHandle(self.handle)
        self.handle = -1

    def __enter__(self) -> WindowsWorkdirLease:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_exclusive(path: Path, lease_id: str) -> WindowsWorkdirLease:
    """Take the exclusive lease on an existing artifact, or refuse without waiting."""
    handle = _KERNEL32.CreateFileW(
        str(path),
        GENERIC_READ,
        LEASE_SHARE_MODE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE:
        # The Bridge pre-creates every configured artifact, so anything that cannot be opened here
        # is missing, planted, or denied — all of them fail-closed conditions (§5.2).
        raise BridgeError("LOCK_PATH_UNSAFE", "workdir lease path is unavailable")
    if not _is_regular_on_handle(handle):
        _KERNEL32.CloseHandle(handle)
        raise BridgeError("LOCK_PATH_UNSAFE", "workdir lease path is invalid")
    if not _KERNEL32.LockFileEx(
        handle,
        LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
        0,
        _FULL_RANGE,
        _FULL_RANGE,
        ctypes.byref(_zeroed_overlapped()),
    ):
        code = ctypes.get_last_error()
        _KERNEL32.CloseHandle(handle)
        if code == ERROR_LOCK_VIOLATION:
            raise BridgeError("WORKDIR_BUSY", f"{describe(lease_id)} is busy")
        raise BridgeError("LOCK_PATH_UNSAFE", "workdir lease path is unavailable")
    return WindowsWorkdirLease(handle=handle, path=path, lease_id=lease_id)


def _is_regular_on_handle(handle: int) -> bool:
    info = _AttributeTagInfo()
    ok = _KERNEL32.GetFileInformationByHandleEx(
        handle,
        _FILE_ATTRIBUTE_TAG_INFO,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        return False
    if info.FileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT):
        return False
    return not info.ReparseTag
