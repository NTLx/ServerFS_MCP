"""Windows-only containment for one bounded Repo Ops command and its descendants.

Create suspended, assign the retained process handle to a non-inheritable kill-on-close
Job, then resume. No command code runs before assignment; breakaway is never enabled.
The MCP package's supervisor Job helper is intentionally not a Bridge dependency.
"""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO

from .errors import BridgeError
from .repo_ops import CommandResult, RepoCleanupUnproven, _OutputLimitExceeded

_STOP_TIMEOUT_SECONDS = 5.0
_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_ERROR_NO_MORE_FILES = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_kernel32.QueryInformationJobObject.restype = wintypes.BOOL
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
_kernel32.Thread32First.restype = wintypes.BOOL
_kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
_kernel32.Thread32Next.restype = wintypes.BOOL
_kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenThread.restype = wintypes.HANDLE
_kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
_kernel32.ResumeThread.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class WindowsCommandJob:
    """An anonymous Job owned exclusively by the bounded-command invocation."""

    def __init__(self) -> None:
        self.handle = _kernel32.CreateJobObjectW(None, None)
        self.assigned = False
        if not self.handle:
            raise OSError("command containment could not be created")
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise OSError("command containment could not be configured")

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        # CPython retains this handle even though it closes the primary thread handle.
        # Refuse an unsupported Popen implementation rather than reopening a reused PID.
        handle = getattr(process, "_handle", None)
        if handle is None or not _kernel32.AssignProcessToJobObject(self.handle, int(handle)):
            raise OSError("suspended command could not be contained")
        self.assigned = True

    def resume(self, process: subprocess.Popen[bytes]) -> None:
        if not self.assigned:
            raise OSError("uncontained command must not run")
        snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        if not snapshot or int(snapshot) == _INVALID_HANDLE_VALUE:
            raise OSError("suspended command threads could not be inspected")
        resumed = False
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            present = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if entry.th32OwnerProcessID == process.pid:
                    thread = _kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise OSError("suspended command thread could not be opened")
                    try:
                        if _kernel32.ResumeThread(thread) != 1:
                            raise OSError("command thread was not suspended exactly once")
                        resumed = True
                    finally:
                        _kernel32.CloseHandle(thread)
                present = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != _ERROR_NO_MORE_FILES or not resumed:
                raise OSError("suspended command threads could not be resumed")
        finally:
            _kernel32.CloseHandle(snapshot)

    def active_processes(self) -> int:
        info = _Accounting()
        if not _kernel32.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise RepoCleanupUnproven()
        return info.ActiveProcesses

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        """Synchronous stop proof cannot be interrupted by asyncio cancellation."""
        try:
            if not self.assigned:
                # Assignment failed before resume: this suspended process has no children.
                process.kill()
                process.wait(timeout=_STOP_TIMEOUT_SECONDS)
                return
            if not _kernel32.TerminateJobObject(self.handle, 1):
                raise RepoCleanupUnproven()
            deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
            # Membership cannot be broken and breakaway is disabled. After termination,
            # zero active members proves no command execution can continue. Process-handle
            # signalling/object teardown can lag behind this execution-stop barrier.
            while self.active_processes():
                if time.monotonic() >= deadline:
                    raise RepoCleanupUnproven()
                time.sleep(0.01)
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepoCleanupUnproven() from exc

    def close(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


def _read_limited(stream: BinaryIO, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := stream.read(min(65_536, limit + 1 - total)):
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise _OutputLimitExceeded
    return b"".join(chunks)


async def _drain(tasks: tuple[asyncio.Task, ...], completion: asyncio.Future) -> None:
    settled = asyncio.gather(*tasks, return_exceptions=True)
    cancelled = False
    while not settled.done():
        try:
            await asyncio.shield(settled)
        except asyncio.CancelledError:
            cancelled = True
    # Retrieve the original aggregate's exception as well as all worker exceptions.
    completion.exception()
    if cancelled:
        raise asyncio.CancelledError


async def run_windows_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
    strict_utf8: bool,
) -> CommandResult:
    """Keep containment through pipe draining, including when the leader exits first."""
    try:
        job = WindowsCommandJob()
    except OSError as exc:
        raise BridgeError("REPO_EXEC_FAILED", "repository containment could not start") from exc
    process = None
    tasks: tuple[asyncio.Task, ...] = ()
    try:
        try:
            # No await occurs between creation and assignment/resume. In particular, cancellation
            # cannot strand an uncontained process. Threads only drain stdio and wait afterwards.
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW,
            )
            job.assign(process)
            job.resume(process)
        except FileNotFoundError as exc:
            raise BridgeError(
                "REPO_TOOL_UNAVAILABLE", "required repository tool is unavailable"
            ) from exc
        except OSError as exc:
            raise BridgeError("REPO_EXEC_FAILED", "repository operation could not start") from exc

        assert process.stdout is not None and process.stderr is not None
        tasks = (
            asyncio.create_task(asyncio.to_thread(_read_limited, process.stdout, max_output_bytes)),
            asyncio.create_task(asyncio.to_thread(_read_limited, process.stderr, max_output_bytes)),
            asyncio.create_task(asyncio.to_thread(process.wait)),
        )
        completion = asyncio.gather(*tasks)
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, stderr, returncode = await asyncio.shield(completion)
        except _OutputLimitExceeded as exc:
            raise BridgeError(
                "REPO_OUTPUT_TOO_LARGE", "repository operation output exceeded its limit"
            ) from exc
        except TimeoutError as exc:
            raise BridgeError("REPO_TIMEOUT", "repository operation timed out") from exc
    finally:
        try:
            if process is not None:
                # Also stop descendants that close inherited stdio and outlive a successful leader.
                job.stop(process)
        finally:
            job.close()
        try:
            if tasks:
                await _drain(tasks, completion)
        finally:
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    try:
        return CommandResult(
            returncode=returncode,
            stdout=stdout.decode("utf-8", errors="strict" if strict_utf8 else "replace"),
            stderr=stderr.decode("utf-8", errors="strict" if strict_utf8 else "replace"),
        )
    except UnicodeDecodeError as exc:
        raise BridgeError(
            "REPO_OUTPUT_ENCODING_INVALID", "repository operation emitted non-UTF-8 output"
        ) from exc
