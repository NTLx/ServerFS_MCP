"""Windows-only ACP task process-tree stop proof."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_ERROR_NO_MORE_FILES = 18
_ERROR_INVALID_PARAMETER = 87
_ERROR_NOT_FOUND = 1168
_MAX_PATH = 260


class _PROCESSENTRY32W(ctypes.Structure):
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
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def stop_windows_process_tree(root_pid: int, timeout: float) -> bool:
    """Terminate and prove absence of one task-owned process tree.

    Descendants are identified only by the Windows process snapshot parent-PID relation. Handles
    stay open until the proof finishes so a terminated PID cannot be reused inside the proof window.
    Any access or wait uncertainty fails closed.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    known: set[int] = {root_pid}
    handles: dict[int, int] = {}
    try:
        while True:
            try:
                parents = _snapshot_parents()
            except OSError:
                return False
            _extend_descendants(known, parents)

            for pid in sorted(known):
                if pid in handles or pid not in parents:
                    continue
                handle = _open_for_stop(pid)
                if handle is False:
                    return False
                if handle is not None:
                    handles[pid] = handle

            for handle in handles.values():
                state = _wait_state(handle, 0)
                if state is None:
                    return False
                if state:
                    continue
                if not _kernel32.TerminateProcess(handle, 1):
                    # Windows can refuse termination for a task-owned companion that is already
                    # leaving with its parent. A zero-time check is too early to distinguish that
                    # race from a genuinely unkillable process. Keep the handle and require it to
                    # become signalled within the existing bounded timeout; otherwise fail closed.
                    state = _wait_state(handle, 0)
                    if state is None:
                        return False

            all_stopped = True
            for handle in handles.values():
                state = _wait_state(handle, 0)
                if state is not True:
                    all_stopped = False
                    break

            try:
                after = _snapshot_parents()
            except OSError:
                return False
            before = len(known)
            _extend_descendants(known, after)
            discovered_more = len(known) != before
            live_known = any(pid in after for pid in known)

            if all_stopped and not live_known and not discovered_more:
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wait_ms = max(1, min(int(remaining * 1000), 50))
            for handle in handles.values():
                state = _wait_state(handle, wait_ms)
                if state is None:
                    return False
    finally:
        for handle in handles.values():
            _kernel32.CloseHandle(handle)


def _snapshot_parents() -> dict[int, int]:
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            if error == _ERROR_NO_MORE_FILES:
                return {}
            raise ctypes.WinError(error)
        parents: dict[int, int] = {}
        while True:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            if _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                continue
            error = ctypes.get_last_error()
            if error != _ERROR_NO_MORE_FILES:
                raise ctypes.WinError(error)
            return parents
    finally:
        _kernel32.CloseHandle(snapshot)


def _extend_descendants(known: set[int], parents: dict[int, int]) -> None:
    while True:
        additions: set[int] = set()
        for pid, parent_pid in parents.items():
            if pid not in known and parent_pid in known:
                additions.add(pid)
        if not additions:
            return
        known.update(additions)


def _open_for_stop(pid: int) -> int | None | bool:
    handle = _kernel32.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
    if handle:
        return int(handle)
    error = ctypes.get_last_error()
    if error in {_ERROR_INVALID_PARAMETER, _ERROR_NOT_FOUND}:
        return None
    return False


def _wait_state(handle: int, timeout_ms: int) -> bool | None:
    result = _kernel32.WaitForSingleObject(handle, timeout_ms)
    if result == _WAIT_OBJECT_0:
        return True
    if result == _WAIT_TIMEOUT:
        return False
    return None
