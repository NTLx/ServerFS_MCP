"""Opt-in real Darwin HTTP E2E: two isolated Core processes, one owner Bridge.

This deliberately does NOT register or run macOS ServiceManagement jobs.
Both Core processes are launched directly by the test, using their complete
signed-staged manifests and an isolated copied Python runtime. Every artifact
is under a fresh /private/tmp tree; user HOME / installed services are untouched.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from serverfs_mcp.core_bridge_client_v2 import CoreBridgeClientV2, CoreBridgeRemoteError
from serverfs_mcp.darwin_instance_shared_staging import stage_shared_core
from serverfs_mcp.darwin_instances import instance_service_home
from serverfs_mcp.darwin_shared_runtime import create_shared_core_runtime
from serverfs_mcp.instance_identity import InstanceIdentity
from test_darwin_v0250_shared_native_signed_gate_macos import _create_test_runtime_python

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("SERVERFS_ENABLE_SHARED_HTTP_E2E") != "1",
    reason="requires explicit native two-Core HTTP and private Bridge opt-in",
)

_BRIDGE_SRC = Path(__file__).resolve().parents[1] / "agent_bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

from serverfs_agent_bridge.adapters import FakeAdapter  # noqa: E402
from serverfs_agent_bridge.core_grants import (  # noqa: E402
    CoreGrant,
    CoreGrantVerifier,
    core_workdir_policy_digest,
)
from serverfs_agent_bridge.leases import LeaseManager  # noqa: E402
from serverfs_agent_bridge.models import AgentMode  # noqa: E402
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy  # noqa: E402
from serverfs_agent_bridge.protocol import BridgeProtocolServer  # noqa: E402
from serverfs_agent_bridge.service import BridgeService  # noqa: E402
from serverfs_agent_bridge.store import TaskStore  # noqa: E402


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_status(port: int, *, token: str | None = None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", headers=headers, method="GET")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=0.5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (TimeoutError, OSError):
        return 0


async def _wait_for_core(child: subprocess.Popen[bytes], port: int, log: Path) -> None:
    deadline = time.monotonic() + 18
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise AssertionError(
                f"isolated Core exited early ({child.returncode}): "
                f"{log.read_text(errors='replace')[-900:]}"
            )
        if await asyncio.to_thread(_http_status, port) == 401:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("isolated Shared Core HTTP readiness timed out")


async def _mcp_call(port: int, token: str, name: str, params: dict) -> object:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=8, trust_env=False
    ) as http_client:
        transport = streamable_http_client(
            f"http://127.0.0.1:{port}/mcp",
            http_client=http_client,
            terminate_on_close=False,
        )
        async with Client(transport, mode="2026-07-28", raise_exceptions=True) as client:
            if name == "list-tools":
                return {tool.name for tool in (await client.list_tools()).tools}
            return await client.call_tool(name, params)


def _private_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _prepare_core(
    *,
    root: Path,
    home: Path,
    identity: InstanceIdentity,
    bridge_config: Path,
    bridge_socket: Path,
    policy_digest: str,
    workspace: Path,
    template_app: Path,
    python: Path,
    port: int,
    token: str,
    secret: bytes,
) -> tuple[Path, Path]:
    inputs = root / f"input-{identity.instance_id}"
    inputs.mkdir(mode=0o700)
    config = inputs / "core.toml"
    config.write_text(
        "[agent]\nenabled = true\n[agent.codex]\nenabled = true\n"
        "[[workdirs]]\nalias = 'Workspace'\n"
        f"path = '{workspace}'\nread_only = false\n"
        "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n",
        encoding="utf-8",
    )
    env = inputs / "http.env"
    env.write_text(
        f"SERVERFS_HTTP_API_KEY={token}\n"
        f"SERVERFS_HTTP_ALLOWED_HOSTS=core.example.test,127.0.0.1:{port}\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    binding = inputs / "owner-binding.json"
    _private_json(
        binding,
        {
            "schema_version": 1,
            "bridge_id": identity.bridge_id,
            "core_id": identity.instance_id,
            "key_id": f"key-{identity.instance_id}",
            "secret_hex": secret.hex(),
            "workdir_alias": "Workspace",
            "workdir_path": str(workspace),
            "policy_revision": policy_digest,
            "bridge_config_path": str(bridge_config),
            "socket_path": str(bridge_socket),
        },
    )
    stage = stage_shared_core(
        identity, config, env, binding, port, "core.example.test", template_app, python, home=home
    )
    assert stage["state"] == "staged-not-registered"
    return config, instance_service_home(identity, home=home)


def _start_core(
    stack: ExitStack,
    *,
    root: Path,
    home: Path,
    identity: InstanceIdentity,
    config: Path,
    service_home: Path,
    python: Path,
    port: int,
    token: str,
) -> tuple[subprocess.Popen[bytes], Path]:
    log = root / f"{identity.instance_id}.stderr"
    stream = stack.enter_context(log.open("wb"))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON"))
    }
    env.update(
        {
            "HOME": str(home),
            "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
            "SERVERFS_HTTP_API_KEY": token,
            "SERVERFS_HTTP_ALLOWED_HOSTS": f"core.example.test,127.0.0.1:{port}",
            "SERVERFS_DATA_HOME": str(service_home / "data"),
            "SERVERFS_FILE_INGRESS_ENABLED": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    child = subprocess.Popen(
        [
            str(python),
            "-B",
            "-m",
            "serverfs_mcp.cli",
            "serve",
            "--transport",
            "streamable-http",
            "--config",
            str(config),
            "--http-port",
            str(port),
            "--macos-instance",
            identity.instance_id,
            "--macos-shared-bridge",
            str(identity.bridge_id),
        ],
        env=env,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stream,
        start_new_session=True,
    )

    def stop() -> None:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    stack.callback(stop)
    return child, log


@pytest.mark.parametrize(
    "runtime_kind",
    [
        "copied-test",
        pytest.param(
            "offline-wheel",
            marks=pytest.mark.skipif(
                os.environ.get("SERVERFS_ENABLE_SHARED_LOCKED_HTTP_E2E") != "1",
                reason="requires explicit full offline wheel runtime HTTP E2E opt-in",
            ),
        ),
    ],
)
def test_two_real_shared_core_http_processes_use_one_owner_bridge(
    runtime_kind: str,
) -> None:
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="sfsh3-", dir="/tmp") as scratch:
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
            assert uv_path is not None, "full offline Shared Core E2E requires uv"
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
        build = subprocess.run(
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
        assert build.returncode == 0, "isolated signed app build failed"
        template = root / "native-build/ServerFSServiceManager.app"

        bridge_id = "test-owner"
        bridge_socket = root / "b.sock"
        bridge_config = root / "bridge.json"
        _private_json(
            bridge_config,
            {
                "protocol_mode": "v2",
                "bridge_id": bridge_id,
                "socket_path": str(bridge_socket),
            },
        )
        policy = WorkdirAgentPolicy(
            slot=1,
            alias="Workspace",
            host_path=workspace,
            mode=AgentMode.WORKSPACE_WRITE,
            runtimes=frozenset({"fake"}),
            read_only=False,
        )
        digest = core_workdir_policy_digest(policy)
        registry = PolicyRegistry([policy])
        state = root / "bridge-state"
        store = TaskStore(state)
        bridge_service = BridgeService(
            store=store,
            policies=registry,
            adapters={"fake": FakeAdapter()},
            lease_manager=LeaseManager(root / "locks"),
        )
        secret_a, secret_b = bytes(range(32)), bytes(range(32, 64))
        identities = [
            InstanceIdentity("core-a", "shared", bridge_id),
            InstanceIdentity("core-b", "shared", bridge_id),
        ]
        grants = (
            CoreGrant(
                identity.instance_id,
                f"key-{identity.instance_id}",
                secret,
                (("Workspace", digest),),
                frozenset({"bridge.ping", "task.submit", "task.get", "task.events"}),
            )
            for identity, secret in zip(identities, (secret_a, secret_b), strict=True)
        )
        server = BridgeProtocolServer(
            service=bridge_service,
            socket_path=bridge_socket,
            allowed_peer_uid=os.getuid(),
            v2_only=True,
            core_grant_verifier=CoreGrantVerifier(
                bridge_id, tuple(grants), nonce_claim=store.claim_core_nonce
            ),
        )
        ports = _port(), _port()
        while ports[0] == ports[1]:
            ports = _port(), _port()
        tokens = ("ephemeral-alpha-only-key", "ephemeral-bravo-only-key")

        async def run() -> None:
            await server.start()
            try:
                assert bridge_socket.exists()
                with ExitStack() as stack:
                    processes = []
                    for index, identity in enumerate(identities):
                        config, service_home = _prepare_core(
                            root=root,
                            home=home,
                            identity=identity,
                            bridge_config=bridge_config,
                            bridge_socket=bridge_socket,
                            policy_digest=digest,
                            workspace=workspace,
                            template_app=template,
                            python=python,
                            port=ports[index],
                            token=tokens[index],
                            secret=(secret_a, secret_b)[index],
                        )
                        processes.append(
                            _start_core(
                                stack,
                                root=root,
                                home=home,
                                identity=identity,
                                config=config,
                                service_home=service_home,
                                python=python,
                                port=ports[index],
                                token=tokens[index],
                            )
                        )
                    for index, (child, log) in enumerate(processes):
                        await _wait_for_core(child, ports[index], log)
                        assert (
                            await asyncio.to_thread(
                                _http_status, ports[index], token=tokens[1 - index]
                            )
                            == 401
                        )

                    for i in (0, 1):
                        tools = await _mcp_call(ports[i], tokens[i], "list-tools", {})
                        assert isinstance(tools, set)
                        assert "get_agent_task" in tools
                        result = await _mcp_call(ports[i], tokens[i], "list_workdirs", {})
                        assert result.structured_content is not None
                        assert {w["alias"] for w in result.structured_content["workdirs"]} == {
                            "Workspace"
                        }

                    direct_a = CoreBridgeClientV2(
                        bridge_socket,
                        bridge_id=bridge_id,
                        core_id=identities[0].instance_id,
                        key_id="key-core-a",
                        secret=secret_a,
                        workdir_alias="Workspace",
                        policy_revision=digest,
                        timeout_seconds=5,
                    )
                    direct_b = CoreBridgeClientV2(
                        bridge_socket,
                        bridge_id=bridge_id,
                        core_id=identities[1].instance_id,
                        key_id="key-core-b",
                        secret=secret_b,
                        workdir_alias="Workspace",
                        policy_revision=digest,
                        timeout_seconds=5,
                    )
                    submitted = await direct_a.call(
                        "task.submit",
                        {
                            "runtime": "fake",
                            "path": ".",
                            "profile": "review",
                            "prompt": "synthetic local task",
                        },
                    )
                    task_id = submitted["task_id"]
                    fetched_a = await _mcp_call(
                        ports[0], tokens[0], "get_agent_task", {"task_id": task_id}
                    )
                    assert not fetched_a.is_error
                    assert fetched_a.structured_content["task_id"] == task_id
                    fetched_b = await _mcp_call(
                        ports[1], tokens[1], "get_agent_task", {"task_id": task_id}
                    )
                    assert fetched_b.is_error
                    with pytest.raises(CoreBridgeRemoteError) as forbidden:
                        await direct_b.call("task.get", {"task_id": task_id})
                    assert forbidden.value.code == "CORE_TASK_NOT_FOUND"

                    # One Core is a separate process. Terminating it does not
                    # revoke the other Core or stop the shared owner Bridge.
                    first, _ = processes[0]
                    first.terminate()
                    await asyncio.to_thread(first.wait, 5)
                    assert first.returncode is not None
                    assert bridge_socket.is_socket()
                    assert processes[1][0].poll() is None
                    assert await direct_b.call("bridge.ping", {}) == {"status": "ok"}
                    assert (
                        await _mcp_call(ports[1], tokens[1], "get_agent_task", {"task_id": task_id})
                    ).is_error
            finally:
                await server.close()
            assert not bridge_socket.exists()

        asyncio.run(run())
        assert not bridge_socket.exists()
        assert not any(path.name.startswith("shgate") for path in instances.iterdir())
