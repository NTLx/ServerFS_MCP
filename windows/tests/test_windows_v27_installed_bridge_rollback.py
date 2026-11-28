"""Real installed Windows Bridge rollback; no Scheduler or live provider.

Historical source comes from a controller-verified exact Git archive.
Both versions use private locked environments and the same synthetic
Named Pipe/Job identity and persisted task store. This gate is opt-in.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import replace
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

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("SERVERFS_WINDOWS_INSTALLED_ROLLBACK") != "1",
    reason="requires explicit isolated Windows installed rollback opt-in",
)

ROOT = Path(__file__).resolve().parents[1]


def _install(source: Path, destination: Path) -> Path:
    uv = shutil.which("uv")
    assert uv is not None, "uv unavailable"
    assert source.is_dir()
    manifest = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["project"]["name"] == "serverfs-agent-bridge"
    assert manifest["project"]["version"] == "0.26.0"
    destination.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON", "UV_PROJECT"))
        and key.upper() not in {"VIRTUAL_ENV", "CONDA_PREFIX", "UV_WORKING_DIR"}
    }
    env["UV_OFFLINE"] = "1"
    env["UV_LINK_MODE"] = "copy"

    def run(*argv: str) -> None:
        result = subprocess.run(
            list(argv),
            cwd=source,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=240,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, result.stderr[-3000:]

    wheels = destination / "wheels"
    requirements = destination / "requirements.txt"
    venv = destination / "venv"
    run(uv, "build", "--offline", "--wheel", "--out-dir", str(wheels))
    built = list(wheels.glob("serverfs_agent_bridge-0.26.0-*.whl"))
    assert len(built) == 1
    wheel = built[0]
    with zipfile.ZipFile(wheel) as bundle:
        assert bundle.testzip() is None
    print(f"INSTALLED_ROLLBACK_WHEEL {wheel.name} {hashlib.sha256(wheel.read_bytes()).hexdigest()}")
    run(
        uv,
        "export",
        "--offline",
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--output-file",
        str(requirements),
    )
    run(uv, "venv", "--offline", "--no-project", "--python", sys._base_executable, str(venv))
    python = venv / "Scripts/python.exe"
    run(uv, "pip", "sync", "--offline", "--python", str(python), str(requirements))
    run(uv, "pip", "install", "--offline", "--no-deps", "--python", str(python), str(wheel))
    run(
        str(python),
        "-I",
        "-B",
        "-c",
        "import serverfs_agent_bridge as b; import sys; from pathlib import Path; "
        "assert Path(b.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())",
    )
    return python


def test_real_installed_bridge_upgrade_and_rollback_preserves_tasks(tmp_path: Path) -> None:
    source = Path(os.environ["SERVERFS_WINDOWS_BASELINE_BRIDGE_SOURCE"]).resolve(strict=True)
    assert not source.is_relative_to(ROOT)
    baseline = _install(source, tmp_path / "baseline")
    candidate = _install(ROOT / "agent_bridge", tmp_path / "candidate")
    owner = tmp_path / "owner"
    owner.mkdir()
    plan, credentials = _test_installation(owner, worker=baseline, fake=True)
    config_bytes = plan.config_path.read_bytes()
    state_identity = plan.state_dir.stat().st_ino
    retained: dict[str, str] = {}

    def selected(worker: Path, marker: str | None) -> None:
        active = replace(plan, bridge_python=worker)
        process = None
        with WindowsJob(active.job_name) as job:
            try:
                process = spawn_bridge(
                    config_path=active.config_path,
                    child_env=bridge_child_environment(None),
                    job=job,
                    argv=[
                        str(worker),
                        "-I",
                        "-B",
                        "-m",
                        "serverfs_agent_bridge.main",
                        "--config",
                        str(active.config_path),
                        "--supervised",
                    ],
                )
                assert process.stdin is not None
                frame = bootstrap_frame_bytes(None, prior_bridge_execution_stopped=True)
                assert process.stdin.write(frame) == len(frame)
                process.stdin.flush()
                asyncio.run(
                    wait_for_signed_bridge_v2_ready(
                        active, process, server_pid_validator=job.contains_pid, timeout=15.0
                    )
                )

                def client(core_id: str) -> CoreGrantBridgeClient:
                    return CoreGrantBridgeClient(
                        Path(active.pipe_name),
                        core_id=core_id,
                        bridge_id=active.bridge_id,
                        key_id="key-" + core_id[-1],
                        secret=credentials[core_id],
                        policy_revisions={"Repo": active.health_policy_digest},
                        server_pid_validator=job.contains_pid,
                        timeout_seconds=3.0,
                    )

                async def exercise() -> None:
                    own = client("core-a")
                    peer = client("core-b")
                    catalog = await own.call("runtime.list", {"workdir": "Repo"})
                    assert [item["name"] for item in catalog["runtimes"]] == ["fake"]
                    for task_id, expected in retained.items():
                        params = {"workdir": "Repo", "task_id": task_id}
                        task = await own.call("task.get", params)
                        assert task["status"] == "succeeded"
                        assert task["final_response"] == expected
                        with pytest.raises(AgentBridgeRemoteError) as denied:
                            await peer.call("task.get", params)
                        assert denied.value.code == "CORE_TASK_NOT_FOUND"
                    if marker is not None:
                        submitted = await own.call(
                            "task.submit",
                            {
                                "runtime": "fake",
                                "workdir": "Repo",
                                "path": "",
                                "profile": "workspace-write",
                                "prompt": f"complete:{marker}",
                            },
                        )
                        task_id = submitted["task_id"]
                        for _ in range(100):
                            task = await own.call(
                                "task.get", {"workdir": "Repo", "task_id": task_id}
                            )
                            if task["status"] == "succeeded":
                                assert task["final_response"] == marker
                                retained[task_id] = marker
                                break
                            assert task["status"] not in {"failed", "cancelled", "timed_out"}
                            await asyncio.sleep(0.05)
                        else:
                            pytest.fail("synthetic installed Bridge task did not finish")
                    discovered = await own.call("task.list", {"workdir": "Repo", "status": "all"})
                    assert {task["task_id"] for task in discovered["tasks"]} == set(retained)
                    foreign = await peer.call("task.list", {"workdir": "Repo", "status": "all"})
                    assert foreign == {"tasks": [], "next_cursor": None}

                asyncio.run(exercise())
                request_graceful_shutdown(process)
                assert await_graceful_exit(process, timeout=10)
                assert process.returncode == 0
            finally:
                if process is not None and process.poll() is None:
                    request_graceful_shutdown(process)
                    await_graceful_exit(process, timeout=2)
        assert process is not None and process.poll() is not None
        assert plan.config_path.read_bytes() == config_bytes
        assert plan.state_dir.stat().st_ino == state_identity

    selected(baseline, "baseline-result")
    selected(candidate, "candidate-result")
    selected(baseline, None)
    assert len(retained) == 2
