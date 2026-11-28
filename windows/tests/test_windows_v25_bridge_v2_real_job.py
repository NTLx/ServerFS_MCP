"""Windows v25 independent Bridge Job peer identity: real kernel check.

The only spawned child is a suspended, no-network Python sleeper inside
a random test-only named Job. No Bridge provider, Scheduler, production
service, user token or permanent installation is touched.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys

import pytest

from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_job import WindowsJob
from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator
from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources

if sys.platform != "win32":
    pytest.skip("Windows v2 named Job membership requires the kernel", allow_module_level=True)


def test_v25_core_queries_only_its_bridge_job_without_adopting_child():
    bridge_id = "job-test-" + secrets.token_hex(6)
    sid = current_user_sid()
    resources = WindowsBridgeResources(sid, bridge_id)
    validator = make_bridge_peer_validator(bridge_id=bridge_id, user_sid=sid)
    child = None
    assigned = False

    with WindowsJob(resources.job_name) as job:
        try:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import time;time.sleep(30)",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_SUSPENDED", 0x4)
                ),
            )
            job.assign(child)
            assigned = True
            job.resume(child)
            assert job.contains_pid(child.pid)
            assert validator(child.pid) is True
            assert validator(os.getpid()) is False
        finally:
            # Only an uncontained child is manually killed. A contained child
            # must be reaped by kill-on-Job-close, not by the test.
            if not assigned and child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert child is not None
    child.wait(timeout=10)
    assert validator(child.pid) is False
