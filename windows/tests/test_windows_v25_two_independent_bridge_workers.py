"""Two genuinely separate Windows Bridge workers have independent Job lifetimes.

No Task Scheduler, installed service, public Connector or real Agent provider.
Both workers are disposable subprocesses with independently provisioned HMAC
grants, private state, read-only workdirs, named pipes and Windows named Jobs.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from serverfs_mcp.agent_client import AgentBridgeRemoteError
from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
from serverfs_mcp.agent_lifecycle import (
    await_graceful_exit,
    bootstrap_frame_bytes,
    bridge_child_environment,
    request_graceful_shutdown,
    spawn_bridge,
)
from serverfs_mcp.windows_job import WindowsJob
from serverfs_mcp.windows_v25.bridge_v2_owner import wait_for_signed_bridge_v2_ready
from test_windows_v25_bridge_v2_real_worker import _test_installation

if sys.platform != "win32":
    pytest.skip("independent Bridge workers require native Windows Jobs", allow_module_level=True)


def _client(plan, *, key: bytes, validator) -> CoreGrantBridgeClient:
    return CoreGrantBridgeClient(
        Path(plan.pipe_name),
        bridge_id=plan.bridge_id,
        core_id="core-a",
        key_id="key-a",
        secret=key,
        policy_revisions={"Repo": plan.health_policy_digest},
        server_pid_validator=validator,
    )


def _ping(client: CoreGrantBridgeClient) -> dict:
    return asyncio.run(client.call("bridge.ping", {"workdir": "Repo"}))


def test_two_independent_bridge_workers_survive_peer_shutdown(tmp_path: Path) -> None:
    installs = []
    for name in ("first", "second"):
        private = tmp_path / name
        private.mkdir()
        installs.append(_test_installation(private))

    (first, first_secrets), (second, second_secrets) = installs
    assert first.bridge_id != second.bridge_id
    assert first.pipe_name != second.pipe_name
    assert first.job_name != second.job_name
    assert first.state_dir != second.state_dir
    assert first_secrets["core-a"] != second_secrets["core-a"]

    workers = []
    with ExitStack() as stack:
        first_job = stack.enter_context(WindowsJob(first.job_name))
        second_job = stack.enter_context(WindowsJob(second.job_name))
        try:
            for plan, job in ((first, first_job), (second, second_job)):
                process = spawn_bridge(
                    config_path=plan.config_path,
                    child_env=bridge_child_environment(None),
                    job=job,
                    argv=[
                        str(plan.bridge_python),
                        "-I",
                        "-B",
                        "-m",
                        "serverfs_agent_bridge.main",
                        "--config",
                        str(plan.config_path),
                        "--supervised",
                    ],
                )
                workers.append(process)
                assert process.stdin is not None
                frame = bootstrap_frame_bytes(None, prior_bridge_execution_stopped=True)
                assert process.stdin.write(frame) == len(frame)
                process.stdin.flush()
                asyncio.run(
                    wait_for_signed_bridge_v2_ready(
                        plan, process, server_pid_validator=job.contains_pid, timeout=15.0
                    )
                )

            first_worker, second_worker = workers
            assert first_worker.poll() is None
            assert second_worker.poll() is None
            assert first_job.contains_pid(first_worker.pid)
            assert second_job.contains_pid(second_worker.pid)
            assert not first_job.contains_pid(second_worker.pid)
            assert not second_job.contains_pid(first_worker.pid)

            first_client = _client(
                first, key=first_secrets["core-a"], validator=first_job.contains_pid
            )
            second_client = _client(
                second, key=second_secrets["core-a"], validator=second_job.contains_pid
            )
            assert _ping(first_client) == {"status": "ok"}
            assert _ping(second_client) == {"status": "ok"}

            # A valid identity from Bridge A cannot access Bridge B,
            # even when both have the same public Core/key ID labels.
            wrong = _client(second, key=first_secrets["core-a"], validator=second_job.contains_pid)
            with pytest.raises(AgentBridgeRemoteError) as denied:
                _ping(wrong)
            assert denied.value.code == "CORE_NOT_AUTHORIZED"

            request_graceful_shutdown(first_worker)
            assert await_graceful_exit(first_worker, timeout=10)
            assert first_worker.returncode == 0
            assert second_worker.poll() is None
            assert second_job.contains_pid(second_worker.pid)
            assert _ping(second_client) == {"status": "ok"}

            request_graceful_shutdown(second_worker)
            assert await_graceful_exit(second_worker, timeout=10)
            assert second_worker.returncode == 0
        finally:
            for process in workers:
                request_graceful_shutdown(process)
                if process.poll() is None:
                    await_graceful_exit(process, timeout=2)

    assert all(process.poll() is not None for process in workers)
