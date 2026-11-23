"""Actual supervised v2 Bridge *worker process* over Windows Job + Named Pipe.

The owner/replay/protocol path is used without a Task Scheduler registration.
All inputs are disposable: randomly derived SID+Bridge ID, private pytest
state, two generated Core keys, read-only workdir, no Agent runtime or
external network. The worker is reaped on every path by its test-only Job.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
import sys
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
from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_job import WindowsJob
from serverfs_mcp.windows_private_artifacts import protect_new_file
from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources
from serverfs_mcp.windows_v25.bridge_v2_owner import (
    NativeBridgeV2Plan,
    wait_for_signed_bridge_v2_ready,
)
from serverfs_mcp.windows_v25.core_bridge_binding import parse_core_bridge_binding
from serverfs_mcp.windows_v25.spec import Manifest

if sys.platform != "win32":
    pytest.skip("real v2 worker process is Windows-only", allow_module_level=True)

_WORKER = Path(__file__).resolve().parents[1] / "agent_bridge" / ".venv" / "Scripts" / "python.exe"
# The provisioning step uses the installed Bridge package itself to calculate
# the full canonical workdir policy digest and write its protected grant file.
# The test never reimplements the digest or weakens Windows private DACLs.
_PROVISION = """
import json
import sys
from pathlib import Path
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
from serverfs_agent_bridge.core_grants_private import provision_private_core_grants
from serverfs_agent_bridge.store import TaskStore
config = BridgeConfig.load(Path(sys.argv[1]))
document = json.load(sys.stdin)
digest = core_workdir_policy_digest(config.policies.get("Repo"))
for grant in document["grants"]:
    grant["workdirs"][0]["policy_sha256"] = digest
store = TaskStore(config.state_dir)
provision_private_core_grants(
    store=store,
    bridge_id=config.bridge_id,
    policies=config.policies,
    document=document,
)
print(digest)
"""


def _test_installation(tmp_path: Path):
    if not _WORKER.is_file():
        pytest.skip("separate local Bridge worker venv is not installed")
    sid = current_user_sid()
    bridge_id = "v2-worker-" + secrets.token_hex(5)
    resource = WindowsBridgeResources(sid, bridge_id)
    workdir = tmp_path / "read-only"
    workdir.mkdir()
    home = tmp_path / "bridge-private"
    config_file = tmp_path / "isolated-v2-bridge.json"
    config_file.write_text(
        json.dumps(
            {
                "bridge_id": bridge_id,
                "core_grants_enabled": True,
                "lease_key": "alias",
                "socket_path": resource.pipe_name,
                "state_dir": str(home / "state"),
                "lock_dir": str(home / "locks"),
                "allowed_peer_sid": sid,
                "workdirs": [
                    {
                        "alias": "Repo",
                        "host_path": str(workdir),
                        "read_only": True,
                        "agent_mode": "disabled",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # The named v2 owner now rechecks the protected config DACL before startup.
    # Keep the real-worker fixture faithful to provisioned private artifacts.
    protect_new_file(config_file, sid)
    secrets_by_core = {
        "core-a": secrets.token_bytes(32),
        "core-b": secrets.token_bytes(32),
    }
    document = {
        "schema_version": 1,
        "bridge_id": bridge_id,
        "grants": [
            {
                "core_id": core_id,
                "key_id": "key-" + core_id[-1],
                "secret_hex": key.hex(),
                "methods": ["bridge.ping", "runtime.list"],
                "workdirs": [{"alias": "Repo", "policy_sha256": "0" * 64}],
            }
            for core_id, key in secrets_by_core.items()
        ],
    }
    result = subprocess.run(
        [str(_WORKER), "-I", "-B", "-c", _PROVISION, str(config_file)],
        input=json.dumps(document),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
        env=bridge_child_environment(None),
    )
    # No unredacted private grant input is included in errors, output or logs.
    assert result.returncode == 0, "Bridge owner-private provisioning failed"
    digest = result.stdout.strip()
    assert len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
    plan = NativeBridgeV2Plan(
        bridge_id=bridge_id,
        config_path=config_file,
        bridge_python=_WORKER,
        state_dir=home / "state",
        lock_dir=home / "locks",
        pipe_name=resource.pipe_name,
        job_name=resource.job_name,
        lease_artifact=resource.lifecycle_artifact_name,
        health_core_id="core-a",
        health_key_id="key-a",
        health_workdir="Repo",
        health_policy_digest=digest,
        health_secret=secrets_by_core["core-a"],
    )
    return plan, secrets_by_core


def test_supervised_independent_v2_bridge_worker_job_and_two_core_signed_pipe(tmp_path: Path):
    plan, secrets_by_core = _test_installation(tmp_path)
    sid = current_user_sid()
    bridge = None
    with WindowsJob(plan.job_name) as job:
        try:
            bridge = spawn_bridge(
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
            assert bridge.stdin is not None
            frame = bootstrap_frame_bytes(None, prior_bridge_execution_stopped=True)
            assert bridge.stdin.write(frame) == len(frame)
            bridge.stdin.flush()
            asyncio.run(
                wait_for_signed_bridge_v2_ready(
                    plan, bridge, server_pid_validator=job.contains_pid, timeout=15.0
                )
            )
            # I3 shared mode: two separately parsed Core owner identities
            # produce their own signed client against one real resident worker.
            bridge_owner = Manifest.parse(
                {
                    "schema": 2,
                    "component": "bridge",
                    "instance_id": plan.bridge_id,
                    "sid": sid,
                    "pythonw": str(Path(sys.executable).with_name("pythonw.exe")),
                    "settings": {
                        "config": str(plan.config_path),
                        "bridge_python": str(plan.bridge_python),
                    },
                }
            )
            for index, (core_id, secret) in enumerate(secrets_by_core.items()):
                owner = Manifest.parse(
                    {
                        "schema": 2,
                        "component": "core",
                        "instance_id": core_id,
                        "sid": sid,
                        "pythonw": str(tmp_path / core_id / "Scripts" / "pythonw.exe"),
                        "settings": {
                            "config": str(tmp_path / core_id / "serverfs.toml"),
                            "auth_file": str(tmp_path / core_id / "http.env"),
                            "port": 18869 + index,
                            "hostname": f"{core_id}.example.test",
                        },
                    }
                )
                private_binding = {
                    "schema_version": 1,
                    "instance_id": core_id,
                    "bridge_mode": "shared",
                    "bridge_id": plan.bridge_id,
                    "key_id": "key-" + core_id[-1],
                    "secret_hex": secret.hex(),
                    "workdirs": [{"alias": "Repo", "policy_sha256": plan.health_policy_digest}],
                }
                binding = parse_core_bridge_binding(
                    owner,
                    private_binding,
                    local_aliases=frozenset({"Repo"}),
                    verified_bridge=bridge_owner,
                    bridge_lock_dir=plan.lock_dir,
                )
                settings, client = binding.build_wiring(peer_validator=job.contains_pid)
                assert settings.agent_bridge_socket == plan.pipe_name
                assert binding.instance_id == core_id
                assert binding.bridge_mode == "shared"
                assert asyncio.run(client.call("bridge.ping", {"workdir": "Repo"})) == {
                    "status": "ok"
                }
                assert asyncio.run(client.call("runtime.list", {})) == {"runtimes": []}
            invalid = CoreGrantBridgeClient(
                Path(plan.pipe_name),
                core_id="core-a",
                bridge_id=plan.bridge_id,
                key_id="key-a",
                secret=secrets_by_core["core-b"],
                policy_revisions={"Repo": plan.health_policy_digest},
                server_pid_validator=job.contains_pid,
            )
            with pytest.raises(AgentBridgeRemoteError) as denied:
                asyncio.run(invalid.call("bridge.ping", {"workdir": "Repo"}))
            assert denied.value.code == "CORE_NOT_AUTHORIZED"
            request_graceful_shutdown(bridge)
            assert await_graceful_exit(bridge, timeout=10), "v2 worker ignored stdin EOF"
            assert bridge.returncode == 0
        finally:
            # The holding Windows Job is the final containment guarantee.
            if bridge is not None:
                request_graceful_shutdown(bridge)
                if bridge.poll() is None:
                    await_graceful_exit(bridge, timeout=2)
    assert bridge is not None and bridge.poll() is not None


def test_v25_native_owner_entry_runs_real_v2_worker_and_releases_its_lease(
    tmp_path: Path, monkeypatch
):
    """Exercise the actual v25 owner orchestration without Scheduler writes.

    Only installed-input lookup, cross-instance inventory and publication are
    replaced with private pytest fixtures; Job, lease, bootstrap, Bridge process,
    signed Named Pipe readiness and EOF shutdown all run for real.
    """
    from serverfs_mcp import agent_lifecycle
    from serverfs_mcp.native_lifecycle import LifecycleLease
    from serverfs_mcp.windows_v25 import bridge_v2_owner, readiness, runtime, storage
    from serverfs_mcp.windows_v25 import cli as owner_cli
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.workdir_ownership import WorkdirClaim

    plan, _credentials = _test_installation(tmp_path)
    sid = current_user_sid()
    manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": plan.bridge_id,
            "component": "bridge",
            "sid": sid,
            "pythonw": str(Path(sys.executable).with_name("pythonw.exe")),
            "settings": {
                "config": str(plan.config_path),
                "bridge_python": str(plan.bridge_python),
            },
        }
    )
    observed: list[object] = []
    spawned = []
    original_spawn = agent_lifecycle.spawn_bridge
    monkeypatch.setattr(bridge_v2_owner, "load_native_bridge_plan", lambda _: plan)
    monkeypatch.setattr(
        bridge_v2_owner,
        "load_owned_bridge_workdir_claims",
        lambda _: (WorkdirClaim(plan.bridge_id, str(tmp_path / "read-only"), False),),
    )
    monkeypatch.setattr(
        owner_cli, "_assert_instance_isolation", lambda _: observed.append("isolation")
    )
    monkeypatch.setattr(
        owner_cli,
        "_legacy_bridge_active",
        lambda: pytest.fail("read-only owner must not inspect legacy Bridge"),
    )
    monkeypatch.setattr(storage, "home", lambda component, instance_id: plan.state_dir.parent)

    def spawn(**kwargs):
        process = original_spawn(**kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(agent_lifecycle, "spawn_bridge", spawn)

    def publish(_manifest):
        assert _manifest == manifest
        observed.append("signed-ready")
        assert len(spawned) == 1
        # Real readiness has already completed. Ask only the temporary
        # worker to shut down; the owner will observe its terminal state.
        request_graceful_shutdown(spawned[0])

    monkeypatch.setattr(readiness, "publish", publish)
    assert runtime.run_independent_bridge_v2(manifest) == 3
    assert observed == ["isolation", "signed-ready"]
    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    # A succeeding owner must be able to reacquire this exact private lease.
    with LifecycleLease(root=plan.state_dir.parent, artifact_name=plan.lease_artifact):
        pass
