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


def _venv_home_pythonw(prefix: Path) -> Path | None:
    """Find the exact CPython base PythonW from one private venv's pyvenv.cfg.

    Used when a separate Connector interpreter verifies the Core listener.
    It must not use that Connector's own sys._base_executable as proof of
    the Core interpreter. An absent, redirected or ambiguous config fails.
    """
    import stat

    config = prefix / "pyvenv.cfg"
    try:
        info = config.lstat()
        if (
            not config.is_file()
            or config.is_symlink()
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not 1 <= info.st_size <= 8192
        ):
            return None
        homes = [
            line.partition("=")[2].strip()
            for line in config.read_text(encoding="utf-8").splitlines()
            if line.partition("=")[0].strip().casefold() == "home"
        ]
        if len(homes) != 1 or not homes[0]:
            return None
        home = Path(homes[0])
        if not home.is_absolute():
            return None
        base = (home / "pythonw.exe").resolve(strict=True)
        if not base.is_file() or base == prefix / "Scripts" / "pythonw.exe":
            return None
        if prefix == base.parent or prefix in base.parents:
            return None
        return base
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return None


def registered_pythonw_image_matches(
    *,
    registered_launcher: Path,
    kernel_image: Path,
    venv_prefix: Path,
    current_executable: Path,
    base_executable: Path | None,
    require_pythonw_entrypoint: bool,
    permit_peer_controller: bool = False,
) -> bool:
    """Attest the *exact* native venv launcher and its CPython base PythonW.

    Runtime owner: current sys.prefix and sys.executable must match the task's
    dedicated PythonW launcher; the kernel image must match either it or
    sys._base_executable's exact sibling pythonw.exe.

    Cross-venv listener inspector: permit only another dedicated Python
    console entrypoint and derive the Core's base from the Core's own private
    pyvenv.cfg, NOT from the inspecting Connector's unrelated interpreter.
    The listener separately verifies task SID, PID, creation time, TCP owner
    and unmodified registered task/XML; this check cannot replace them.
    """
    try:
        current_prefix = venv_prefix.resolve(strict=True)
        owner_prefix = registered_launcher.parent.parent.resolve(strict=True)
        launcher = registered_launcher.resolve(strict=True)
        interpreter = current_executable.resolve(strict=True)
        image = kernel_image.resolve(strict=True)
        if (
            not current_prefix.is_dir()
            or not owner_prefix.is_dir()
            or registered_launcher.name.casefold() != "pythonw.exe"
            or launcher != (owner_prefix / "Scripts" / "pythonw.exe").resolve(strict=True)
            or not launcher.is_file()
        ):
            return False
        expected_name = "pythonw.exe" if require_pythonw_entrypoint else "python.exe"
        expected_interpreter = (current_prefix / "Scripts" / expected_name).resolve(strict=True)
        if interpreter != expected_interpreter or not interpreter.is_file():
            return False
        same_environment = current_prefix == owner_prefix
        if not same_environment and (not permit_peer_controller or require_pythonw_entrypoint):
            return False
        if image == launcher:
            return True
        if same_environment:
            if base_executable is None or not base_executable.is_absolute():
                return False
            base = base_executable.resolve(strict=True)
            if not base.is_file() or base.name.casefold() not in ("python.exe", "pythonw.exe"):
                return False
            base_pythonw = base.with_name("pythonw.exe").resolve(strict=True)
            if not base_pythonw.is_file() or base_pythonw == launcher:
                return False
            if base.parent == owner_prefix or owner_prefix in base.parents:
                return False
        else:
            base_pythonw = _venv_home_pythonw(owner_prefix)
            if base_pythonw is None:
                return False
        return image == base_pythonw
    except (OSError, RuntimeError, ValueError):
        return False


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
    "registered_pythonw_image_matches",
    "same_process",
    "wait_process_gone",
]
