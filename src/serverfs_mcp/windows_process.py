"""Narrow Win32 process identity helpers for native Windows lifecycle handover."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

_WINDOWS = os.name == "nt"
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class WindowsProcessError(Exception):
    """A Windows process identity operation failed."""


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    parent_pid: int
    executable_name: str


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    image_path: Path
    creation_time: int


if _WINDOWS:  # pragma: no cover - exercised on the Windows acceptance host

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


def _kernel32():
    if not _WINDOWS:
        raise WindowsProcessError("Windows process inspection requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def process_entries() -> tuple[ProcessEntry, ...]:
    """Return one process-table snapshot without opening every process."""
    kernel32 = _kernel32()
    handle = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if handle in (None, 0, _INVALID_HANDLE_VALUE):
        raise WindowsProcessError("the Windows process table is unavailable")
    entries: list[ProcessEntry] = []
    try:
        item = _ProcessEntry32W()
        item.dwSize = ctypes.sizeof(item)
        if not kernel32.Process32FirstW(handle, ctypes.byref(item)):
            raise WindowsProcessError("the Windows process table could not be read")
        while True:
            entries.append(
                ProcessEntry(
                    pid=int(item.th32ProcessID),
                    parent_pid=int(item.th32ParentProcessID),
                    executable_name=str(item.szExeFile),
                )
            )
            item.dwSize = ctypes.sizeof(item)
            if not kernel32.Process32NextW(handle, ctypes.byref(item)):
                break
    finally:
        kernel32.CloseHandle(handle)
    return tuple(entries)


def process_identity(pid: int) -> ProcessIdentity | None:
    """Read image + creation time from one held process handle."""
    if pid <= 0:
        return None
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        creation_time = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return ProcessIdentity(
            pid=pid,
            image_path=Path(buffer.value),
            creation_time=creation_time,
        )
    finally:
        kernel32.CloseHandle(handle)


def same_process(identity: ProcessIdentity) -> bool:
    current = process_identity(identity.pid)
    if current is None:
        return False
    return current.creation_time == identity.creation_time and os.path.normcase(
        str(current.image_path)
    ) == os.path.normcase(str(identity.image_path))


def wait_process_gone(identity: ProcessIdentity, timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not same_process(identity):
            return True
        time.sleep(0.1)
    return not same_process(identity)


__all__ = [
    "ProcessEntry",
    "ProcessIdentity",
    "WindowsProcessError",
    "process_entries",
    "process_identity",
    "same_process",
    "wait_process_gone",
]
