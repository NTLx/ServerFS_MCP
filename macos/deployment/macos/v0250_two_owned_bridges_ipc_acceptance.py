"""Two isolated macOS owned Bridge IPC processes, Fake Adapter only.

Explicit --execute is required. No ServiceManagement register, no persistent
Bridge install, no real Agent provider, no default Bridge IPC and no external
network. Every named socket belongs to a random synthetic instance and the
scripts verifies that the four existing production launchd PIDs do not change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from serverfs_mcp.agent_client import (
    AgentBridgeClient,
    AgentBridgeRemoteError,
)
from serverfs_mcp.agent_lifecycle import bootstrap_frame_bytes
from serverfs_mcp.darwin_instance_bridge_identity import (
    named_owned_bridge_resources,
    refuse_bridge_resource_collisions,
    refuse_writable_workdir_overlaps,
)
from serverfs_mcp.darwin_service import _launchd_job_pid
from serverfs_mcp.instance_identity import InstanceIdentity
from serverfs_mcp.workdirs import Workdir

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PYTHON = ROOT / "agent_bridge/.venv/bin/python"
PROTECTED = (
    "com.ntlx.serverfs.tunnel",
    "com.ntlx.serverfs.bridge",
    "com.ntlx.serverfs.http-origin",
    "com.ntlx.serverfs.cloudflare",
)


def _identity(name: str) -> InstanceIdentity:
    return InstanceIdentity(instance_id=name, bridge_mode="owned", bridge_id=name)


def _prepare(home: Path, identity: InstanceIdentity):
    owner = home / identity.instance_id
    owner.mkdir(mode=0o700)
    workdir = owner / "workdir"
    workdir.mkdir(mode=0o700)
    resources = named_owned_bridge_resources(identity, home=home)
    if resources.socket_path.parent.exists() or resources.socket_path.exists():
        raise RuntimeError("synthetic owned Bridge IPC identity already exists")
    for directory in (
        resources.state_dir,
        resources.lock_dir,
        resources.config_path.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    config = {
        "socket_path": str(resources.socket_path),
        "state_dir": str(resources.state_dir),
        "lock_dir": str(resources.lock_dir),
        "lease_key": "alias",
        # Owned Bridges are current-user only. Passing allowed_peer_gid
        # would activate the shared-group socket mode (0660), which is
        # forbidden for this independent 0600 IPC endpoint.
        "allowed_peer_uid": os.getuid(),
        "enable_fake_runtime": True,
        "workdirs": [
            {
                "alias": identity.instance_id,
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["fake"],
            }
        ],
    }
    resources.config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    resources.config_path.chmod(0o600)
    return resources, workdir


def _start(resources, logs: Path):
    if not BRIDGE_PYTHON.is_file() or not os.access(BRIDGE_PYTHON, os.X_OK):
        raise RuntimeError("isolated Bridge interpreter is unavailable")
    with logs.open("wb") as stream:
        process = subprocess.Popen(
            [
                str(BRIDGE_PYTHON),
                "-B",
                "-m",
                "serverfs_agent_bridge.main",
                "--config",
                str(resources.config_path),
                "--supervised",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stream,
            start_new_session=True,
        )
    try:
        assert process.stdin is not None
        process.stdin.write(bootstrap_frame_bytes(None))
        process.stdin.flush()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("synthetic Bridge process exited before IPC readiness")
            if resources.socket_path.is_socket():
                mode = resources.socket_path.stat()
                if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) != 0o600:
                    raise RuntimeError("synthetic Bridge socket is not private")
                return process
            time.sleep(0.1)
        raise RuntimeError("synthetic Bridge socket was not ready")
    except Exception:
        _shutdown(process)
        raise


def _shutdown(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def _probe(owners, resources) -> int:
    alpha, bravo = owners
    a = AgentBridgeClient(resources[0].socket_path, timeout_seconds=5.0)
    b = AgentBridgeClient(resources[1].socket_path, timeout_seconds=5.0)
    for client in (a, b):
        reply = await client.call("runtime.list", {})
        assert any(item["name"] == "fake" for item in reply["runtimes"])
    # The two distinct Bridge policies must not accept the OTHER instance's
    # alias even when both run under the same current-user UID.
    for client, other in ((a, bravo), (b, alpha)):
        try:
            await client.call(
                "task.submit",
                {
                    "runtime": "fake",
                    "workdir": other.instance_id,
                    "path": "",
                    "profile": "review",
                    "prompt": "complete:cross-instance-should-not-run",
                },
            )
        except AgentBridgeRemoteError:
            pass
        else:
            raise AssertionError("a Bridge accepted the other instance's workdir")

    submitted = []
    for client, owner in ((a, alpha), (b, bravo)):
        result = await client.call(
            "task.submit",
            {
                "runtime": "fake",
                "workdir": owner.instance_id,
                "path": "",
                "profile": "review",
                "prompt": f"complete:{owner.instance_id}",
            },
        )
        submitted.append((client, owner.instance_id, result["task_id"]))
    for client, name, task_id in submitted:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = await client.call("task.get", {"task_id": task_id})
            if task["status"] == "succeeded":
                assert task["final_response"] == name
                break
            if task["status"] in {"failed", "cancelled", "interrupted"}:
                raise AssertionError("synthetic isolated Bridge task failed")
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("synthetic isolated Bridge task did not complete")
    # Task identifiers are scoped to the owning Bridge's state store.
    try:
        await b.call("task.get", {"task_id": submitted[0][2]})
    except AgentBridgeRemoteError:
        pass
    else:
        raise AssertionError("another Bridge exposed a foreign task ID")
    return len(submitted)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print("NOT_EXECUTED: explicit --execute required for ephemeral Bridge subprocesses")
        return 2
    baseline = tuple(_launchd_job_pid(label) for label in PROTECTED)
    if any(pid is None for pid in baseline):
        raise RuntimeError("production Bridge/Tunnel/Core baseline is incomplete")

    marker = secrets.token_hex(4)
    owners = (_identity(f"v25a-{marker}"), _identity(f"v25b-{marker}"))
    processes: list[subprocess.Popen[bytes]] = []
    socket_parents: list[Path] = []
    result = "NOT_ACCEPTED"
    count = 0
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-bridge-ipc-") as tmp:
        home = Path(tmp).resolve()
        try:
            prepared = [_prepare(home, item) for item in owners]
            resources = [item[0] for item in prepared]
            socket_parents = [item.socket_path.parent for item in resources]
            refuse_bridge_resource_collisions(resources[0], (resources[1],))
            a_root, b_root = (item[1] for item in prepared)
            refuse_writable_workdir_overlaps(
                (Workdir("alpha", a_root, None, read_only=False),),
                ((Workdir("bravo", b_root, None, read_only=False),),),
            )
            for index, spec in enumerate(resources):
                processes.append(_start(spec, home / f"bridge-{index}.stderr.log"))
            count = asyncio.run(_probe(owners, resources))
            result = "PASS"
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            for process in reversed(processes):
                _shutdown(process)
            for directory in socket_parents:
                if directory.exists():
                    # Do not unlink another process's socket or unexpected
                    # artifacts. Bridge should remove its socket on shutdown.
                    if any(directory.iterdir()):
                        errors.append("synthetic Bridge socket directory is not empty")
                    else:
                        directory.rmdir()
            if tuple(_launchd_job_pid(label) for label in PROTECTED) != baseline:
                errors.append("protected production PID changed")
        print(
            json.dumps(
                {
                    "result": "PASS" if result == "PASS" and not errors else "NOT_ACCEPTED",
                    "isolated_bridge_processes": len(processes),
                    "synthetic_tasks_succeeded": count,
                    "default_production_pids_unchanged": (
                        tuple(_launchd_job_pid(label) for label in PROTECTED) == baseline
                    ),
                    "owned_ipc_cleaned": all(not path.exists() for path in socket_parents),
                    "errors": errors,
                },
                sort_keys=True,
            )
        )
    return 0 if result == "PASS" and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
