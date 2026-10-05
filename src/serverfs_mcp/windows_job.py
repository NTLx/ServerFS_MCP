"""Windows Job Object containment for the Bridge process tree (v0.11 Phase D, §9, §15 D6).

A narrow helper over four Win32 calls: ``CreateJobObjectW``, ``SetInformationJobObject`` with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, ``AssignProcessToJobObject``, and ``CloseHandle``.

Why the Bridge needs containment: the Bridge spawns provider children, and those children spawn
their own. If the supervisor dies abnormally, nothing in that tree closes the pipe or releases the
lease, and a provider process can survive holding a writer lease — which would leave the workdir
permanently ``WORKDIR_BUSY`` with no live owner to explain it. Kill-on-close makes the OS the
reaper: the last handle to the job closes, and the tree goes with it.

Two boundaries are deliberate and are what keep this from becoming a process hijack:

- **The Bridge is the only member.** The tunnel-client and the ServerFS stdio child are explicitly
  not assigned: they are not Bridge-owned, and a job that reached them could kill the operator's
  own MCP session. Provider descendants need no explicit handling — a child created by a process in
  a job is in that job unless it asks to break away, so containment extends naturally while
  staying owned.
- **Assignment failure fails closed.** If ``AssignProcessToJobObject`` fails — a nested-job
  restriction, a privilege boundary, an already-assigned process — an Agent-enabled startup aborts.
  Continuing would mean running the Bridge with the containment the operator was promised, and
  losing it silently at exactly the moment it matters.

Nothing here is Windows-Secret or requires elevation: ``CreateJobObjectW`` and process assignment
are available to an ordinary user process for its own children.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

#: Only meaningful on Windows; the module imports cleanly elsewhere so shared code can be tested.
WINDOWS = sys.platform == "win32"


class JobObjectError(Exception):
    """Job Object containment could not be established; the caller must fail closed.

    The message names the failure class only. A raw handle or Win32 error text is never included,
    because supervisor diagnostics are operator-visible and this is not a place to leak internals.
    """


class WindowsJob:
    """A kill-on-close job holding the Bridge process tree.

    Used as a context manager so the handle cannot leak on an exception path: leaving the process
    is the normal case and must still close the job, because closing it is what guarantees the tree
    is gone.
    """

    def __init__(self) -> None:
        self._handle: int | None = None
        self._closed = False

    # -- construction ---------------------------------------------------

    def open(self) -> None:
        """Create the job and arm kill-on-close. Idempotent."""
        if self._handle is not None:
            return
        self._handle = _create_kill_on_close_job()
        self._closed = False

    def assign(self, process: subprocess.Popen[Any]) -> None:
        """Place one already-started process in this job.

        The process must be started suspended and resumed by the caller if a race-free assignment is
        wanted; this helper does not do that, because the supervisor's startup order (§15 D5) spawns
        the Bridge and assigns immediately, and a Bridge that has not yet bound its pipe cannot have
        served anything.
        """
        if self._handle is None:
            raise JobObjectError("the job object is not open")
        _assign_process(self._handle, process)

    def close(self) -> None:
        """Close the job handle. On Windows this is the kill: the tree dies with the handle."""
        if self._handle is None or self._closed:
            return
        _close_handle(self._handle)
        self._handle = None
        self._closed = True

    @property
    def is_open(self) -> bool:
        return self._handle is not None and not self._closed

    def __enter__(self) -> WindowsJob:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# -- Win32 surface ------------------------------------------------------
# Loaded lazily and only on Windows so this module stays importable (and unit-testable) elsewhere.

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
#: PROCESS_SET_QUOTA | PROCESS_TERMINATE, plus the standard right set, needed to assign a process.
_PROCESS_ALL_ACCESS = 0x1F0FFF

# BOOLEAN(1) + padding + two LARGE_INTEGERs + four DWORDs; see JOBOBJECT_EXTENDED_LIMIT_INFORMATION.
_EXTENDED_LIMIT_SIZE = 144 + 8


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _create_kill_on_close_job() -> int:
    """Create a job object armed with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``.

    Every other limit field is left zero, which means "no further limit applies" — in particular no
    active-process or memory cap, so containment constrains the process *tree* without constraining
    what the Bridge is allowed to use.
    """
    import ctypes

    kernel32 = _kernel32()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise JobObjectError("the job object could not be created")

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
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

    class _ExtendedLimitInfo(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = _ExtendedLimitInfo()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(handle)
        raise JobObjectError("kill-on-close could not be configured on the job object")
    return int(handle)


def _assign_process(handle: int, process: subprocess.Popen[Any]) -> None:
    kernel32 = _kernel32()
    # CPython's Windows Popen exposes the process handle as ``_handle``; there is no public
    # accessor. ``_winapi`` is consulted as the documented fallback so this keeps working if the
    # private name ever moves. A pid is required for that fallback, so its absence is not fatal.
    target = getattr(process, "_handle", None)
    if target is None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0:
            try:
                import _winapi

                target = _winapi.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
            except (ImportError, OSError):
                target = None
    if target is None or int(target) == 0:
        raise JobObjectError("the child process has no usable handle to assign")
    if not kernel32.AssignProcessToJobObject(handle, int(target)):
        # A nested-job restriction or a privilege boundary lands here. Agent-enabled startup must
        # fail closed rather than run the Bridge unconstrained (§15 D6).
        raise JobObjectError("the Bridge process could not be assigned to the job object")


def _close_handle(handle: int) -> None:
    _kernel32().CloseHandle(handle)


__all__ = ["WINDOWS", "JobObjectError", "WindowsJob"]
