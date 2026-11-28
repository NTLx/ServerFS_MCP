"""Opt-in W2 native Job isolation: kill one owned child, preserve the peer.

Both jobs are anonymous and contain ONLY the harmless Python subprocesses
spawned by this test. Never join the named Bridge Job or touch Providers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import suppress

import pytest

from serverfs_mcp.windows_job import WindowsJob

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object only"),
    pytest.mark.skipif(
        os.environ.get("SERVERFS_W2_JOB_ISOLATION") != "1",
        reason="explicit W2 isolated process containment opt-in required",
    ),
]


def _contained_child(job: WindowsJob) -> subprocess.Popen[bytes]:
    # Reuse the product's already-reviewed suspended-create/assign/resume
    # path instead of assuming undocumented CPython constant exports.
    from pathlib import Path

    from serverfs_mcp.windows_cloudflare_runtime import (
        _launch,
        _minimal_windows_environment,
    )

    executable = Path(sys.executable)
    return _launch(
        executable,
        ["-I", "-B", "-c", "import time; time.sleep(40)"],
        env=_minimal_windows_environment(),
        cwd=executable.parent,
        job=job,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        popen=subprocess.Popen,
    )


def _reap_own_child(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        with suppress(OSError):
            process.kill()
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=5)


def test_killing_one_anonymous_job_does_not_kill_other_job_child() -> None:
    first = WindowsJob()
    second = WindowsJob()
    child_a: subprocess.Popen[bytes] | None = None
    child_b: subprocess.Popen[bytes] | None = None
    try:
        first.open()
        second.open()
        child_a = _contained_child(first)
        child_b = _contained_child(second)
        assert child_a.poll() is None
        assert child_b.poll() is None
        # W2d: the actual Windows kernel must bind readiness to the held
        # anonymous Job, not to same-user SID or a guessed launcher PID.
        assert first.contains_pid(child_a.pid)
        assert second.contains_pid(child_b.pid)
        assert not first.contains_pid(child_b.pid)
        assert not second.contains_pid(child_a.pid)
        assert not first.contains_pid(os.getpid())

        first.close()
        assert child_a.wait(timeout=10) is not None
        assert child_b.poll() is None, "independent peer process was terminated"

        second.close()
        assert child_b.wait(timeout=10) is not None
    finally:
        first.close()
        second.close()
        _reap_own_child(child_a)
        _reap_own_child(child_b)
