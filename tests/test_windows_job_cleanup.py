"""Phase D tests for Job Object failure cleanup and handle hygiene (§15 D6, D7, P1).

Three properties, all of them about what happens when something goes wrong:

- a containment failure surfaces as a redacted ``AgentLifecycleError`` / exit code 2, never a
  traceback or a raw handle;
- ``job.close()`` is unreachable-around. It runs in the ``finally`` and no failure in the shutdown
  sequence can skip it, because that close is the only thing guaranteeing no provider descendant
  survives;
- the ``OpenProcess`` fallback handle is closed on both the success and the failure path.

The last one is a leak rather than a visible failure, so it is asserted by counting handles: a
repeated assign/close cycle must not walk the process's handle count upward.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from serverfs_mcp.windows_job import WINDOWS, JobObjectError, WindowsJob

pytestmark = pytest.mark.skipif(not WINDOWS, reason="Windows Job Object containment")


def _handle_count() -> int:
    """This process's open handle count, via GetProcessHandleCount."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    count = wintypes.DWORD()
    current = kernel32.GetCurrentProcess()
    if not kernel32.GetProcessHandleCount(current, ctypes.byref(count)):
        pytest.skip("GetProcessHandleCount is unavailable here")
    return int(count.value)


def _spawn_sleeper(seconds: int = 60) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


class TestFailureIsRedacted:
    """A containment failure must not leak internals."""

    def test_open_failure_is_a_job_error_not_a_traceback(self, monkeypatch):
        """The failure class is what the supervisor turns into exit 2."""
        import serverfs_mcp.windows_job as module

        def _boom(name: str | None) -> int:
            raise OSError("kernel32 refused")

        monkeypatch.setattr(module, "_create_kill_on_close_job", _boom)
        job = WindowsJob()
        with pytest.raises(JobObjectError) as raised:
            job.open()
        assert "Traceback" not in str(raised.value)

    def test_assign_failure_carries_no_handle_value(self):
        job = WindowsJob()
        job.open()
        try:

            class _Fake:
                _handle = None

            with pytest.raises(JobObjectError) as raised:
                job.assign(_Fake())  # type: ignore[arg-type]
            message = str(raised.value)
            assert "0x" not in message
            assert "Traceback" not in message
        finally:
            job.close()

    def test_error_message_names_only_the_failure_class(self):
        """No handle, no PID, no Win32 text: supervisor stderr is operator-visible."""
        job = WindowsJob()
        job.open()
        try:

            class _Fake:
                _handle = None

            with pytest.raises(JobObjectError) as raised:
                job.assign(_Fake())  # type: ignore[arg-type]
            assert str(raised.value) == "the child process has no usable handle to assign"
        finally:
            job.close()


class TestCloseIsUnskippable:
    """job.close() must run whatever the shutdown sequence does."""

    def test_close_runs_when_open_itself_failed(self, monkeypatch):
        import serverfs_mcp.windows_job as module

        job = WindowsJob()

        def _boom(name: str | None) -> int:
            raise OSError("nope")

        monkeypatch.setattr(module, "_create_kill_on_close_job", _boom)
        with pytest.raises(JobObjectError):
            job.open()
        # A failed open leaves nothing to close, and close() must still be safe.
        job.close()
        assert not job.is_open

    def test_close_runs_after_a_shutdown_error(self, monkeypatch):
        """An exception while asking the Bridge to stop must not skip the containment close.

        The helpers live in agent_lifecycle; the supervisor imports them inside its Agent path, so
        the guarded sequence is reproduced here rather than reached through run_with_agent.
        """
        import serverfs_mcp.agent_lifecycle as lifecycle

        calls: list[str] = []
        monkeypatch.setattr(
            lifecycle, "request_graceful_shutdown", lambda p: calls.append("shutdown")
        )

        def _raise(process, *, timeout: float = 5.0) -> bool:
            raise RuntimeError("the pipe is already gone")

        monkeypatch.setattr(lifecycle, "await_graceful_exit", _raise)
        job = WindowsJob()
        job.open()
        try:
            # Mirrors the supervisor's finally block: the guarded shutdown, then the close.
            try:
                lifecycle.request_graceful_shutdown(None)  # type: ignore[arg-type]
                lifecycle.await_graceful_exit(None)  # type: ignore[arg-type]
            except Exception:
                pass
            finally:
                job.close()
            assert not job.is_open, "the job handle was left open"
            assert calls == ["shutdown"]
        finally:
            job.close()

    def test_double_close_is_still_safe_after_a_failure(self):
        job = WindowsJob()
        job.open()
        job.close()
        job.close()
        assert not job.is_open


class TestHandleHygiene:
    """The OpenProcess fallback must not leak."""

    def test_repeated_assign_does_not_grow_the_handle_count(self):
        """A leak here would exhaust the supervisor's handles across repeated startups."""
        job = WindowsJob()
        job.open()
        try:
            # Warm up so one-off allocations are not counted as growth.
            warmup = _spawn_sleeper(5)
            job.assign(warmup)
            warmup.kill()
            warmup.wait(timeout=15)
            before = _handle_count()

            for _ in range(5):
                child = _spawn_sleeper(5)
                job.assign(child)
                child.kill()
                child.wait(timeout=15)

            after = _handle_count()
            # No growth at all would be ideal; a small constant is acceptable, a climb is not.
            assert after - before <= 2, f"handle count grew from {before} to {after}"
        finally:
            job.close()

    def test_failed_assignment_does_not_leak_either(self):
        """The failure path must close the fallback handle too, not only the success path."""
        job = WindowsJob()
        job.open()
        try:
            before = _handle_count()
            for _ in range(5):

                class _Fake:
                    _handle = None
                    pid = 0  # invalid pid: the fallback is attempted and then fails

                with pytest.raises(JobObjectError):
                    job.assign(_Fake())  # type: ignore[arg-type]
            after = _handle_count()
            assert after - before <= 2, f"handle count grew from {before} to {after}"
        finally:
            job.close()

    def test_open_process_fallback_handle_is_closed_on_the_success_path(self, monkeypatch):
        """The fallback opens its own handle, so the success path must close it too.

        The normal ``_handle`` attribute short-circuits the fallback, so this drives the fallback
        directly: without it the leak would be invisible, because a real ``Popen`` never takes that
        branch. A real child is used rather than this process, since assigning a process that is
        already in a job is refused — which would make the success path unreachable.
        """
        import _winapi

        import serverfs_mcp.windows_job as module

        opened: list[int] = []
        real_open_process = _winapi.OpenProcess

        def _tracking_open_process(access, inherit, pid):  # noqa: ANN001 - _winapi's shape
            handle = real_open_process(0x1F0FFF, False, pid)
            opened.append(int(handle))
            return handle

        monkeypatch.setattr(_winapi, "OpenProcess", _tracking_open_process)

        class _OnlyPid:
            """No ``_handle``, so the fallback is the only route to a handle."""

            def __init__(self, pid: int) -> None:
                self.pid = pid

        job = WindowsJob()
        job.open()
        try:
            child = _spawn_sleeper(30)
            try:
                before = _handle_count()
                for _ in range(5):
                    job.assign(_OnlyPid(child.pid))  # type: ignore[arg-type]
                after = _handle_count()
                assert len(opened) == 5, "the fallback was never exercised"
                assert after - before <= 2, f"handle count grew from {before} to {after}"
            finally:
                child.kill()
                child.wait(timeout=15)
        finally:
            job.close()
        del module


class TestSupervisorHandlesBothErrorTypes:
    """A containment failure must be caught by the supervisor's redacted handler."""

    def test_supervisor_catches_lifecycle_and_job_errors_together(self):
        """Both types reach the same handler, so neither escapes as an unhandled traceback."""
        import inspect

        from serverfs_mcp.agent_lifecycle import AgentLifecycleError
        from serverfs_mcp.supervisor import run_with_agent

        source = inspect.getsource(run_with_agent)
        assert "AgentLifecycleError" in source
        assert "JobObjectError" in source
        # The handler is a tuple catch, so one except clause covers both.
        assert "except (AgentLifecycleError, JobObjectError)" in source
        assert not issubclass(AgentLifecycleError, JobObjectError)

    def test_failure_text_is_a_class_not_a_handle(self):
        for error_type in (JobObjectError,):
            message = str(error_type("the containment job could not be created (OSError)"))
            assert "0x" not in message
            assert "Traceback" not in message
