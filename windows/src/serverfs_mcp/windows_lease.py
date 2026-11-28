"""Windows twin of the mutation-side lease reader: ``LockFileEx`` over the Bridge's artifact.

The shape is frozen by Phase 0A (§5.5, ``docs/windows-phase-0a-lockfileex-2026-10-04.md``): the
reader opens the existing artifact with ``GENERIC_READ`` and ``OPEN_EXISTING`` — never a create
disposition — validates the object on that *same* handle (an attribute-only handle cannot lock at
all), and takes an exclusive, non-blocking, full-range ``LockFileEx``. A conflicting
``CreateFileW`` never blocks, so a busy lease cannot stall a mutation.

Nothing here logs or surfaces a Win32 error code, a handle value or a path: the reader's public
vocabulary is the same three codes POSIX already produced.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wintypes
from collections.abc import Iterator
from pathlib import Path

from .errors import AgentLeaseError, WorkdirBusyError
from .lease_identity import lock_artifact_name, validate_lease_id

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x8000_0000
FILE_SHARE_READ = 0x0000_0001
FILE_SHARE_WRITE = 0x0000_0002
FILE_SHARE_DELETE = 0x0000_0004
READER_SHARE_MODE = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x0000_0080
FILE_ATTRIBUTE_DIRECTORY = 0x0000_0010
FILE_ATTRIBUTE_REPARSE_POINT = 0x0000_0400
INVALID_FILE_ATTRIBUTES = 0xFFFF_FFFF

LOCKFILE_EXCLUSIVE_LOCK = 0x0000_0002
LOCKFILE_FAIL_IMMEDIATELY = 0x0000_0001
_FULL_RANGE = 0xFFFF_FFFF

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_SHARING_VIOLATION = 32
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
_KERNEL32.GetFileAttributesW.restype = wintypes.DWORD
_KERNEL32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]

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
    and the same call with a real structure succeeded.
    """
    return _Overlapped()


def _open_reader(path: Path) -> int:
    handle = _KERNEL32.CreateFileW(
        str(path),
        GENERIC_READ,
        READER_SHARE_MODE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle is not None and handle != _INVALID_HANDLE:
        return handle
    code = ctypes.get_last_error()
    if code in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        # The Bridge owns creation (§5.2), so an absent artifact is a fail-closed condition.
        raise AgentLeaseError("shared Agent workdir lease is unavailable")
    if code == ERROR_SHARING_VIOLATION:
        raise WorkdirBusyError
    raise AgentLeaseError("shared Agent workdir lease is not accessible")


def _verify_regular(handle: int) -> None:
    """The artifact must be a real file, not a directory and not a reparse point (§5.5 item 4)."""
    info = _AttributeTagInfo()
    ok = _KERNEL32.GetFileInformationByHandleEx(
        handle,
        _FILE_ATTRIBUTE_TAG_INFO,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    unsafe = (not ok) or bool(
        info.FileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
    )
    if unsafe or info.ReparseTag:
        raise AgentLeaseError("shared Agent workdir lease is not a regular file")


def _lock(handle: int) -> None:
    if _KERNEL32.LockFileEx(
        handle,
        LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
        0,
        _FULL_RANGE,
        _FULL_RANGE,
        ctypes.byref(_zeroed_overlapped()),
    ):
        return
    code = ctypes.get_last_error()
    if code == ERROR_LOCK_VIOLATION:
        raise WorkdirBusyError
    raise AgentLeaseError("shared Agent workdir lease is unavailable")


@contextlib.contextmanager
def hold(lock_dir: Path, lease_id: str) -> Iterator[None]:
    """Hold the exclusive lease on one workdir artifact for the duration of the block.

    The handle is owned here and closed on every path; releasing the lock is not required for
    correctness (the OS reclaims it at close, even after ``TerminateProcess``) but is done so that a
    graceful release never depends on garbage collection.
    """
    handle = _open_reader(lock_dir / lock_artifact_name(validate_lease_id(lease_id)))
    try:
        _verify_regular(handle)
        _lock(handle)
        try:
            yield
        finally:
            _KERNEL32.UnlockFileEx(
                handle, 0, _FULL_RANGE, _FULL_RANGE, ctypes.byref(_zeroed_overlapped())
            )
    finally:
        _KERNEL32.CloseHandle(handle)


def guard_state(guard_path: Path) -> bool | None:
    """``None`` when no guard is present, ``True`` for a usable one, ``False`` for an unsafe one.

    The path is read by attribute rather than opened: the guard's content belongs to the Bridge, and
    the mutation reader only needs to know that something is there, and that it is a file rather
    than a planted reparse object.
    """
    attributes = _KERNEL32.GetFileAttributesW(str(guard_path))
    if attributes == INVALID_FILE_ATTRIBUTES:
        code = ctypes.get_last_error()
        if code in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
            return None
        raise AgentLeaseError("shared Agent recovery guard is unavailable")
    if attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT):
        return False
    return True
