"""Phase D tests for Windows Job Object containment (§9, §15 D6).

The property under test is not "a job object can be created" — it is that closing the job kills the
whole assigned tree, and that an *unrelated* process survives. A containment mechanism that
over-reaches is worse than none, because it would take the operator's own MCP session down with it.

The tree case uses a real grandchild so the assertion is about inherited containment rather than
about the directly assigned process. The grandchild is identified by writing its PID to a file,
because a job kills a tree that has already lost its parent and there is no other handle to wait on.
"""

from __future__ import annotations

import os
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

    def test_a_named_job_that_already_exists_is_refused(self) -> None:
        """The containment barrier: an existing name means the old object is still there.

        ``CreateJobObjectW`` answers ``ERROR_ALREADY_EXISTS`` *with* a usable handle to the existing
        object, so the natural mistake is to carry on and use it -- which would put the new Bridge
        inside a job the previous generation still owns and make every downstream containment claim
        false. Refusing is the only safe reading.
        """
        name = f"Global\\serverfs-agent-bridge-job-v1-pin-{os.getpid()}"
        holder = WindowsJob(name)
        holder.open()
        try:
            with pytest.raises(JobObjectError, match="already exists"):
                WindowsJob(name).open()
        finally:
            holder.close()

    def test_the_name_is_free_again_once_the_last_handle_closes(self) -> None:
        """The other half: the barrier must not outlive its owner, or nothing could ever restart."""
        name = f"Global\\serverfs-agent-bridge-job-v1-reuse-{os.getpid()}"
        first = WindowsJob(name)
        first.open()
        first.close()
        second = WindowsJob(name)
        second.open()
        try:
            assert second.is_open
        finally:
            second.close()

    def test_the_refusal_does_not_keep_the_old_object_alive(self) -> None:
        """A refused open must not hold a handle: that would keep the name taken after the owner
        exits.

        Observed through the name itself: with the holder already closed, the refused attempt must
        leave the name free for the next caller.
        """
        name = f"Global\\serverfs-agent-bridge-job-v1-nohold-{os.getpid()}"
        holder = WindowsJob(name)
        holder.open()
        with pytest.raises(JobObjectError):
            WindowsJob(name).open()
        holder.close()
        reuser = WindowsJob(name)
        reuser.open()
        try:
            assert reuser.is_open
        finally:
            reuser.close()

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


_MEMBER_SCRIPT = "\n".join(
    [
        "import os, sys, time",
        "path = sys.argv[1]",
        "scratch = path + '.tmp'",
        "n = 0",
        "while True:",
        "    with open(scratch, 'w', encoding='utf-8') as handle:",
        "        handle.write(str(n))",
        "    os.replace(scratch, path)",
        "    n += 1",
        "    time.sleep(0.05)",
    ]
)

#: The owner of the job and, through it, of the member. It is a separate process so that it can be
#: hard-killed -- the supervisor-crash shape, where no handle outlives the owner.
_OWNER_SCRIPT = "\n".join(
    [
        "import subprocess, sys, time",
        "from serverfs_mcp.windows_job import WindowsJob",
        "",
        "job = WindowsJob(sys.argv[1])",
        "job.open()",
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]])",
        "job.assign(child)",
        "print('ready', child.pid, flush=True)",
        "time.sleep(600)",
    ]
)


def _name_is_free(name: str) -> bool:
    """Whether a fresh job of this name can be created. The handle is never kept."""
    job = WindowsJob(name)
    try:
        job.open()
    except JobObjectError:
        return False
    job.close()
    return True


def _still_running(pid: int) -> bool:
    """Whether the process object has not signalled yet -- the diagnostic, not the assertion."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) != 0  # 0 == WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


class TestTheNamedJobBarrierStopsExecution:
    """What the recovery decision actually rests on, observed at the safety boundary.

    The supervisor reads "the previous generation's execution has stopped" out of a fresh named
    Job. So the property to pin is not a timestamp ordering between two kernel events but the
    question the recovery guard is really asking: once the name is free again, can a former
    member still execute user-mode code?

    The member keeps rewriting a counter, which is the cheapest observable user-mode side effect. It
    must visibly advance *before* the kill -- otherwise "unchanged after" would pass because
    nothing ever wrote -- and then be frozen across a window far longer than the measured
    object-teardown gap. Whether the member's process object had already signalled at the barrier
    instant is recorded in the failure message as a diagnostic, because it is interesting and it
    is explicitly *not* the
    claim: termination is asynchronous on Windows, and the guard exists to stop a second writer
    meeting a first one that is still executing, not to wait for a corpse.
    """

    def test_a_former_member_stops_writing_once_the_name_is_free_again(
        self, tmp_path: Path
    ) -> None:
        counter = tmp_path / "counter.txt"
        counter.write_text("-1", encoding="utf-8")
        name = rf"Global\serverfs-agent-bridge-job-v1-counter-{os.getpid()}"

        owner = subprocess.Popen(
            [sys.executable, "-c", _OWNER_SCRIPT, name, str(counter), _MEMBER_SCRIPT],
            stdout=subprocess.PIPE,
        )
        try:
            assert owner.stdout is not None
            ready = owner.stdout.readline().split()
            assert ready[0] == b"ready", f"the owner never came up: {ready}"
            member_pid = int(ready[1])

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and int(counter.read_text(encoding="utf-8")) < 3:
                time.sleep(0.05)
            assert int(counter.read_text(encoding="utf-8")) >= 3, (
                "the member never wrote, so the second half of this test would be vacuous"
            )

            owner.kill()
            owner.wait(timeout=30)

            # The barrier instant: the first moment a fresh job of this name can exist. Create and
            # release, never hold -- this is an observation, not an ownership claim.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not _name_is_free(name):
                time.sleep(0.001)
            assert _name_is_free(name), "the name was never free again after the owner died"
            value_at_barrier = int(counter.read_text(encoding="utf-8"))
            member_object_pending = _still_running(member_pid)

            time.sleep(2.0)
            assert int(counter.read_text(encoding="utf-8")) == value_at_barrier, (
                "a former job member was still executing after the name freed "
                f"(process object still pending at the barrier: {member_object_pending})"
            )
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=10)
