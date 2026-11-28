"""Opt-in three-process macOS Shared Core acceptance with owner restarts.

The owner Bridge runs as its own unsupervised, ephemeral local subprocess,
while two independent Shared Cores run as separate local HTTP subprocesses.
No SMAppService registration, existing user HOME, production grant or Tunnel is
touched. The fake Agent runtime and all credentials are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from serverfs_mcp.core_bridge_client_v2 import (
    CoreBridgeClientV2,
    CoreBridgeRemoteError,
    CoreBridgeUnavailable,
)
from serverfs_mcp.darwin_shared_runtime import create_shared_core_runtime
from serverfs_mcp.instance_identity import InstanceIdentity
from test_darwin_v0250_shared_native_signed_gate_macos import _create_test_runtime_python
from test_darwin_v0250_shared_two_core_http_macos import (
    _http_status,
    _mcp_call,
    _port,
    _prepare_core,
    _private_json,
    _start_core,
    _wait_for_core,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("SERVERFS_ENABLE_SHARED_HTTP_E2E") != "1",
    reason="requires explicit private three-process Shared Core E2E opt-in",
)

_BRIDGE_SRC = Path(__file__).resolve().parents[1] / "agent_bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

from serverfs_agent_bridge.config import BridgeConfig  # noqa: E402
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest  # noqa: E402
from serverfs_agent_bridge.core_grants_private import (  # noqa: E402
    provision_private_core_grants,
)
from serverfs_agent_bridge.store import TaskStore  # noqa: E402


def _stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def _start_owner(
    stack: ExitStack, source: Path, root: Path, home: Path, config: Path, serial: int
) -> tuple[subprocess.Popen[bytes], Path]:
    log = root / f"bridge-{serial}.stderr"
    stream = stack.enter_context(log.open("wb"))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON"))
    }
    env.update(
        {
            "HOME": str(home),
            "PYTHONPATH": str(source / "agent_bridge" / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    child = subprocess.Popen(
        [sys.executable, "-B", "-m", "serverfs_agent_bridge.main", "--config", str(config)],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stream,
        start_new_session=True,
    )
    stack.callback(_stop, child)
    return child, log


async def _wait_owner(
    child: subprocess.Popen[bytes],
    log: Path,
    probe: CoreBridgeClientV2,
    *,
    timeout: float = 14,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise AssertionError(
                f"private Bridge exited early ({child.returncode}): "
                f"{log.read_text(errors='replace')[-700:]}"
            )
        try:
            if await probe.call("bridge.ping", {}) == {"status": "ok"}:
                return
        except CoreBridgeUnavailable:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError("private owner Bridge did not become ready")


@pytest.mark.parametrize(
    "runtime_kind",
    [
        "copied-test",
        pytest.param(
            "offline-wheel",
            marks=pytest.mark.skipif(
                os.environ.get("SERVERFS_ENABLE_SHARED_LOCKED_OWNER_E2E") != "1",
                reason="requires explicit full offline wheel runtime owner restart opt-in",
            ),
        ),
    ],
)
def test_three_native_processes_recover_bridge_without_core_restart(
    runtime_kind: str,
) -> None:
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="sfsh3p-", dir="/tmp") as scratch:
        root = Path(scratch).resolve(strict=True)
        root.chmod(0o700)
        home = root / "home"
        instances = home / "Library/Application Support/ServerFS/instances"
        instances.mkdir(parents=True, mode=0o700)
        instances.chmod(0o700)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)

        if runtime_kind == "offline-wheel":
            uv_path = shutil.which("uv")
            assert uv_path is not None, "offline locked runtime requires installed uv"
            installed = create_shared_core_runtime(
                root / "locked-core-runtime",
                source,
                Path(sys.executable),
                Path(uv_path).resolve(strict=True),
            )
            python = Path(installed["python"])
            assert not python.is_symlink()
        else:
            python = _create_test_runtime_python(root, source)
        built = subprocess.run(
            [
                "bash",
                str(source / "native/macos/service_manager/build.sh"),
                str(root / "native-build"),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert built.returncode == 0, "isolated signed app template build failed"
        template = root / "native-build/ServerFSServiceManager.app"

        bridge_id = "test-owner"
        socket_path = root / "b.sock"
        bridge_config = root / "bridge.json"
        state = root / "bridge-state"
        _private_json(
            bridge_config,
            {
                "socket_path": str(socket_path),
                "protocol_mode": "v2",
                "bridge_id": bridge_id,
                "state_dir": str(state),
                "lock_dir": str(root / "bridge-locks"),
                "allowed_peer_uid": os.getuid(),
                "enable_fake_runtime": True,
                "workdirs": [
                    {
                        "slot": 1,
                        "alias": "Workspace",
                        "host_path": str(workspace),
                        "read_only": False,
                        "agent_mode": "workspace-write",
                        "agent_runtimes": ["fake"],
                    }
                ],
            },
        )
        config = BridgeConfig.load(bridge_config)
        digest = core_workdir_policy_digest(config.policies.get("Workspace"))
        identities = (
            InstanceIdentity("core-a", "shared", bridge_id),
            InstanceIdentity("core-b", "shared", bridge_id),
        )
        secrets = (bytes(range(32)), bytes(range(32, 64)))
        provision_private_core_grants(
            store=TaskStore(state),
            bridge_id=bridge_id,
            policies=config.policies,
            document={
                "schema_version": 1,
                "bridge_id": bridge_id,
                "grants": [
                    {
                        "core_id": identity.instance_id,
                        "key_id": f"key-{identity.instance_id}",
                        "secret_hex": secret.hex(),
                        "methods": ["bridge.ping", "task.submit", "task.get", "task.events"],
                        "workdirs": [{"alias": "Workspace", "policy_sha256": digest}],
                    }
                    for identity, secret in zip(identities, secrets, strict=True)
                ],
            },
        )

        direct = [
            CoreBridgeClientV2(
                socket_path,
                bridge_id=bridge_id,
                core_id=identity.instance_id,
                key_id=f"key-{identity.instance_id}",
                secret=secret,
                workdir_alias="Workspace",
                policy_revision=digest,
                timeout_seconds=4,
            )
            for identity, secret in zip(identities, secrets, strict=True)
        ]
        ports = _port(), _port()
        while ports[0] == ports[1]:
            ports = _port(), _port()
        tokens = ("owner-synthetic-alpha", "owner-synthetic-bravo")

        async def exercise() -> None:
            with ExitStack() as stack:
                owner, owner_log = _start_owner(stack, source, root, home, bridge_config, 1)
                await _wait_owner(owner, owner_log, direct[0])
                assert socket_path.is_socket()

                cores: list[tuple[subprocess.Popen[bytes], Path]] = []
                for i, identity in enumerate(identities):
                    core_config, service_home = _prepare_core(
                        root=root,
                        home=home,
                        identity=identity,
                        bridge_config=bridge_config,
                        bridge_socket=socket_path,
                        policy_digest=digest,
                        workspace=workspace,
                        template_app=template,
                        python=python,
                        port=ports[i],
                        token=tokens[i],
                        secret=secrets[i],
                    )
                    cores.append(
                        _start_core(
                            stack,
                            root=root,
                            home=home,
                            identity=identity,
                            config=core_config,
                            service_home=service_home,
                            python=python,
                            port=ports[i],
                            token=tokens[i],
                        )
                    )
                for i, (child, log) in enumerate(cores):
                    await _wait_for_core(child, ports[i], log)
                    assert (
                        await asyncio.to_thread(_http_status, ports[i], token=tokens[1 - i]) == 401
                    )

                # Both Core APIs are real signed-staged HTTP MCP processes.
                for i in (0, 1):
                    names = await _mcp_call(ports[i], tokens[i], "list-tools", {})
                    assert "get_agent_task" in names
                    result = await _mcp_call(ports[i], tokens[i], "list_workdirs", {})
                    assert {wd["alias"] for wd in result.structured_content["workdirs"]} == {
                        "Workspace"
                    }

                # Tasks belong to the authenticated core_id even when both
                # use exactly the same physical workdir and shared Bridge.
                tasks = []
                for i in (0, 1):
                    issued = await direct[i].call(
                        "task.submit",
                        {
                            "runtime": "fake",
                            "path": ".",
                            "profile": "review",
                            "prompt": f"synthetic-{i}",
                        },
                    )
                    tasks.append(issued["task_id"])
                    own = await _mcp_call(
                        ports[i], tokens[i], "get_agent_task", {"task_id": tasks[i]}
                    )
                    assert not own.is_error
                    assert own.structured_content["task_id"] == tasks[i]
                with pytest.raises(CoreBridgeRemoteError) as cross:
                    await direct[1].call("task.get", {"task_id": tasks[0]})
                assert cross.value.code == "CORE_TASK_NOT_FOUND"

                # Core A is not the owner. Core B and owner remain alive.
                _stop(cores[0][0])
                assert cores[0][0].poll() is not None
                assert owner.poll() is None
                assert cores[1][0].poll() is None
                assert await direct[1].call("bridge.ping", {}) == {"status": "ok"}

                # A true OWNER crash/restart cannot kill Core B. Its file
                # services stay available while Bridge is down, and Agent
                # RPC recovers on next request without restarting Core B.
                _stop(owner)
                assert owner.poll() is not None
                assert cores[1][0].poll() is None
                still_live = await _mcp_call(ports[1], tokens[1], "list_workdirs", {})
                assert still_live.structured_content is not None
                with pytest.raises(CoreBridgeUnavailable):
                    await direct[1].call("bridge.ping", {})

                owner_next, log_next = _start_owner(stack, source, root, home, bridge_config, 2)
                await _wait_owner(owner_next, log_next, direct[1])
                assert owner_next.poll() is None
                assert cores[1][0].poll() is None
                restored = await _mcp_call(
                    ports[1], tokens[1], "get_agent_task", {"task_id": tasks[1]}
                )
                assert not restored.is_error
                assert restored.structured_content["task_id"] == tasks[1]

                # Owner-only GRANT ROTATION: the new grant file is installed
                # while the owner process is stopped. The still-running Core
                # B deliberately continues using its old, now invalid key.
                _stop(owner_next)
                rotated_secret = bytes(range(64, 96))
                owner_grants = [
                    {
                        "core_id": identities[i].instance_id,
                        "key_id": (
                            f"key-{identities[i].instance_id}" if i == 0 else "key-core-b-rotated"
                        ),
                        "secret_hex": (secrets[i] if i == 0 else rotated_secret).hex(),
                        "methods": ["bridge.ping", "task.submit", "task.get", "task.events"],
                        "workdirs": [{"alias": "Workspace", "policy_sha256": digest}],
                    }
                    for i in (0, 1)
                ]
                provision_private_core_grants(
                    store=TaskStore(state),
                    bridge_id=bridge_id,
                    policies=config.policies,
                    document={
                        "schema_version": 1,
                        "bridge_id": bridge_id,
                        "grants": owner_grants,
                    },
                )
                rotated_owner, rotated_log = _start_owner(
                    stack, source, root, home, bridge_config, 3
                )
                await _wait_owner(rotated_owner, rotated_log, direct[0])
                assert cores[1][0].poll() is None
                with pytest.raises(CoreBridgeRemoteError) as stale:
                    await direct[1].call("bridge.ping", {})
                assert stale.value.code == "CORE_NOT_AUTHORIZED"
                stale_http = await _mcp_call(
                    ports[1], tokens[1], "get_agent_task", {"task_id": tasks[1]}
                )
                assert stale_http.is_error

                # Core B picks up owner-provisioned private binding ONLY on
                # its own restart; rotating Bridge grants never stops it.
                from serverfs_mcp.darwin_instances import instance_service_home

                core_b_home = instance_service_home(identities[1], home=home)
                binding = core_b_home / "shared-bridge-binding.json"
                binding_doc = json.loads(binding.read_text(encoding="utf-8"))
                binding_doc["key_id"] = "key-core-b-rotated"
                binding_doc["secret_hex"] = rotated_secret.hex()
                replacement_binding = core_b_home / ".binding-rotation.tmp"
                _private_json(replacement_binding, binding_doc)
                os.replace(replacement_binding, binding)

                _stop(cores[1][0])
                core_b, core_b_log = _start_core(
                    stack,
                    root=root,
                    home=home,
                    identity=identities[1],
                    config=root / "input-core-b" / "core.toml",
                    service_home=core_b_home,
                    python=python,
                    port=ports[1],
                    token=tokens[1],
                )
                await _wait_for_core(core_b, ports[1], core_b_log)
                granted_http = await _mcp_call(
                    ports[1], tokens[1], "get_agent_task", {"task_id": tasks[1]}
                )
                assert not granted_http.is_error
                assert granted_http.structured_content["task_id"] == tasks[1]

                # REVOKE Core B: independent owner shutdown + grant file
                # replacement + restart, no Core B stop or file-service outage.
                _stop(rotated_owner)
                provision_private_core_grants(
                    store=TaskStore(state),
                    bridge_id=bridge_id,
                    policies=config.policies,
                    document={
                        "schema_version": 1,
                        "bridge_id": bridge_id,
                        "grants": [owner_grants[0]],
                    },
                )
                revoked_owner, revoked_log = _start_owner(
                    stack, source, root, home, bridge_config, 4
                )
                await _wait_owner(revoked_owner, revoked_log, direct[0])
                assert core_b.poll() is None
                with pytest.raises(CoreBridgeRemoteError) as revoked:
                    await CoreBridgeClientV2(
                        socket_path,
                        bridge_id=bridge_id,
                        core_id=identities[1].instance_id,
                        key_id="key-core-b-rotated",
                        secret=rotated_secret,
                        workdir_alias="Workspace",
                        policy_revision=digest,
                        timeout_seconds=4,
                    ).call("bridge.ping", {})
                assert revoked.value.code == "CORE_NOT_AUTHORIZED"
                assert (
                    await _mcp_call(ports[1], tokens[1], "get_agent_task", {"task_id": tasks[1]})
                ).is_error
                assert (
                    await _mcp_call(ports[1], tokens[1], "list_workdirs", {})
                ).structured_content is not None

        asyncio.run(exercise())
        assert not socket_path.exists()
        assert not any(p.name.startswith("shgate") for p in instances.iterdir())
