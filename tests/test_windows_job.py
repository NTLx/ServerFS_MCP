"""Phase D tests for Windows Job Object containment (§9, §15 D6).

The property under test is not "a job object can be created" — it is that closing the job kills the
whole assigned tree, and that an *unrelated* process survives. A containment mechanism that
over-reaches is worse than none, because it would take the operator's own MCP session down with it.

The tree case uses a real grandchild so the assertion is about inherited containment rather than
about the directly assigned process. The grandchild is identified by writing its PID to a file,
because a job kills a tree that has already lost its parent and there is no other handle to wait on.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from serverfs_mcp.windows_job import WINDOWS, JobObjectError, WindowsJob

pytestmark = pytest.mark.skipif(not WINDOWS, reason="Windows Job Object containment")

#: A child that waits for a go-ahead, then starts a grandchild and records both PIDs.
#:
#: The go-ahead matters and is the reason this fixture is not simpler: job membership is inherited
#: at process-creation time, so a grandchild spawned *before* its parent was assigned never joins
#: the job and legitimately survives. That is a property of Windows, not a containment defect. The
#: supervisor assigns the Bridge immediately after spawn and the Bridge creates provider children
#: afterwards, so the test has to model that order to prove anything.
_TREE_SCRIPT = """
import os, subprocess, sys, time
from pathlib import Path

pidfile, gofile = Path(sys.argv[1]), Path(sys.argv[2])
deadline = time.time() + 60
while not gofile.exists() and time.time() < deadline:
    time.sleep(0.02)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
pidfile.write_text(f"{os.getpid()}\\n{child.pid}\\n", encoding="utf-8")
time.sleep(120)
"""


def _spawn_tree(pidfile: Path, gofile: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _TREE_SCRIPT, str(pidfile), str(gofile)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _pids(pidfile: Path, timeout: float = 15.0) -> tuple[int, int]:
    """Wait for the tree script to publish the child and grandchild PIDs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pidfile.is_file():
            text = pidfile.read_text(encoding="utf-8").strip()
            if text.count("\n") == 1:
                child, grandchild = (int(line) for line in text.splitlines())
                return child, grandchild
        time.sleep(0.05)
    raise AssertionError("the tree fixture never published its PIDs")


def _alive(pid: int) -> bool:
    """Whether a PID is still running. ``OpenProcess`` is used because a zombie is not alive."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _wait_gone(pid: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


class TestJobLifecycle:
    """Creation, idempotence and handle hygiene."""

    def test_open_creates_and_close_releases(self) -> None:
        job = WindowsJob()
        job.open()
        assert job.is_open
        job.close()
        assert not job.is_open

    def test_open_is_idempotent(self) -> None:
        job = WindowsJob()
        job.open()
        job.open()
        assert job.is_open
        job.close()

    def test_close_is_idempotent(self) -> None:
        job = WindowsJob()
        job.open()
        job.close()
        job.close()
        assert not job.is_open

    def test_context_manager_closes_on_exception(self) -> None:
        job = WindowsJob()
        with pytest.raises(RuntimeError), job:
            raise RuntimeError("startup failed after containment was established")
        assert not job.is_open

    def test_assign_before_open_is_refused(self, tmp_path: Path) -> None:
        job = WindowsJob()
        child = _spawn_tree(tmp_path / "never.txt", tmp_path / "go")
        try:
            with pytest.raises(JobObjectError, match="not open"):
                job.assign(child)
        finally:
            child.kill()

    def test_kill_on_close_flag_is_configured(self) -> None:
        """A job without the flag would close cleanly and leave the tree running."""
        job = WindowsJob()
        job.open()
        try:
            import ctypes

            from serverfs_mcp.windows_job import _kernel32

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
                    (name, ctypes.c_uint64)
                    for name in (
                        "ReadOperationCount",
                        "WriteOperationCount",
                        "OtherOperationCount",
                        "ReadTransferCount",
                        "WriteTransferCount",
                        "OtherTransferCount",
                    )
                ]

            class _Info(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BasicLimit),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = _kernel32()
            kernel32.QueryInformationJobObject.restype = ctypes.c_int
            kernel32.QueryInformationJobObject.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            info = _Info()
            returned = ctypes.c_uint32()
            ok = kernel32.QueryInformationJobObject(
                ctypes.c_void_p(job._handle),
                9,
                ctypes.byref(info),
                ctypes.sizeof(info),
                ctypes.byref(returned),
            )
            assert ok, "QueryInformationJobObject failed on our own job"
            assert info.BasicLimitInformation.LimitFlags & 0x00002000
        finally:
            job.close()


class TestContainment:
    """The two properties that make this safe to ship."""

    def test_closing_the_job_kills_the_assigned_tree(self, tmp_path: Path) -> None:
        """Assigned child and its inherited grandchild must both die with the handle."""
        pidfile, gofile = tmp_path / "tree.pids", tmp_path / "go"
        child = _spawn_tree(pidfile, gofile)

        job = WindowsJob()
        job.open()
        job.assign(child)
        # Only now may the child create descendants, mirroring the supervisor's real order: assign
        # the Bridge immediately after spawn, and the Bridge creates provider children later.
        gofile.write_text("go", encoding="utf-8")
        child_pid, grandchild_pid = _pids(pidfile)
        job.close()

        assert _wait_gone(child_pid), "the assigned child survived job close"
        assert _wait_gone(grandchild_pid), "the inherited grandchild survived job close"

    def test_unrelated_process_survives_job_close(self, tmp_path: Path) -> None:
        """Over-reach would kill the operator's own session, so this is asserted explicitly."""
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        pidfile, gofile = tmp_path / "tree.pids", tmp_path / "go"
        assigned = _spawn_tree(pidfile, gofile)

        job = WindowsJob()
        try:
            job.open()
            job.assign(assigned)
            gofile.write_text("go", encoding="utf-8")
            assigned_pid, _ = _pids(pidfile)
            job.close()
            assert _wait_gone(assigned_pid)
            # The bystander was never assigned, so it must still be running.
            assert _alive(bystander.pid), "job close killed an unrelated process"
        finally:
            bystander.kill()
            bystander.wait(timeout=15)

    def test_a_closed_job_does_not_kill_a_later_process(self, tmp_path: Path) -> None:
        """The handle is spent after close, so containment cannot be re-used by accident."""
        pidfile, gofile = tmp_path / "tree.pids", tmp_path / "go"
        first = _spawn_tree(pidfile, gofile)
        job = WindowsJob()
        job.open()
        job.assign(first)
        gofile.write_text("go", encoding="utf-8")
        first_pid, _ = _pids(pidfile)
        job.close()
        assert _wait_gone(first_pid)

        second = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            job.close()  # a second close must be harmless, and must not reach anything
            assert _alive(second.pid)
        finally:
            second.kill()
            second.wait(timeout=15)


class TestAssignmentFailureIsFatal:
    """A Bridge that cannot be contained must not run uncontained."""

    def test_assigning_an_invalid_handle_fails_closed(self) -> None:
        job = WindowsJob()
        job.open()
        try:

            class _Fake:
                _handle = None  # never a usable process handle

            with pytest.raises(JobObjectError, match="no usable handle"):
                job.assign(_Fake())  # type: ignore[arg-type]
        finally:
            job.close()

    def test_error_text_carries_no_handle_value(self) -> None:
        """Supervisor diagnostics are operator-visible; no raw handle belongs in them."""
        job = WindowsJob()

        class _Fake:
            _handle = None

        job.open()
        try:
            with pytest.raises(JobObjectError) as raised:
                job.assign(_Fake())  # type: ignore[arg-type]
        finally:
            job.close()
        message = str(raised.value)
        assert "0x" not in message


class TestPlatformNeutrality:
    """The module imports off Windows so shared code paths stay testable."""

    @pytest.mark.skipif(WINDOWS, reason="asserts the non-Windows shape")
    def test_creates_no_win32_objects_off_windows(self) -> None:
        """The supervisor must never reach the containment path on a non-Windows deployment."""
        assert WINDOWS is False
        job = WindowsJob()
        with pytest.raises(OSError):
            job.open()  # no kernel32 to load
        assert not job.is_open
