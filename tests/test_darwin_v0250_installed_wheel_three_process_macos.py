"""Real three-process HTTP acceptance against independently installed wheels."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from serverfs_mcp.core_bridge_client_v2 import CoreBridgeClientV2
from serverfs_mcp.darwin_instance_shared_manifest import resolve_shared_core_manifest
from serverfs_mcp.darwin_instances import instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("SERVERFS_ENABLE_INSTALLED_WHEEL_E2E") != "1",
    reason="requires explicit installed-wheel process acceptance opt-in",
)

_ROOT = Path(__file__).resolve().parents[1]
_BRIDGE_SRC = _ROOT / "agent_bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

from serverfs_agent_bridge.config import BridgeConfig  # noqa: E402
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest  # noqa: E402
from serverfs_agent_bridge.core_grants_private import provision_private_core_grants  # noqa: E402
from serverfs_agent_bridge.local_ipc import max_socket_path_bytes  # noqa: E402
from serverfs_agent_bridge.store import TaskStore  # noqa: E402

from test_darwin_v0250_shared_two_core_http_macos import (  # noqa: E402
    _http_status,
    _mcp_call,
    _port,
    _private_json,
    _wait_for_core,
)


def _stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def _require_short_socket_path(path: Path) -> None:
    """Fail before starting Bridge if the test-owned Unix socket cannot bind."""
    size = len(os.fsencode(path))
    limit = max_socket_path_bytes()
    if size > limit:
        raise ValueError(
            f"test-owned Bridge socket path is {size} bytes (limit {limit}); "
            "use a shorter SERVERFS_V0250_WHEEL_SCRATCH"
        )


def _wheel_provenance(python: Path, module: str, distribution: str, cwd: Path) -> str:
    code = (
        "import importlib, importlib.metadata, json, sys; "
        f"m=importlib.import_module({module!r}); "
        f"d=importlib.metadata.distribution({distribution!r}); "
        "print(json.dumps({'executable':sys.executable,'module':m.__file__,"
        "'version':d.version,'location':str(d.locate_file(''))}))"
    )
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", code],
        cwd=cwd,
        env={"PATH": os.defpath, "HOME": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    # Provenance is bound to the *selected* private venv, not a historical
    # wheelhouse directory name. Both executable and imports must stay inside it.
    venv_root = python.parent.parent.resolve(strict=True)
    installed_location = Path(evidence["location"]).resolve(strict=True)
    imported_module = Path(evidence["module"]).resolve(strict=True)
    assert installed_location.is_relative_to(venv_root)
    assert imported_module.is_relative_to(installed_location)
    return evidence["version"]


def _private_file(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _prepare_core(
    *,
    root: Path,
    home: Path,
    identity: InstanceIdentity,
    config: Path,
    workspace: Path,
    bridge_config: Path,
    bridge_socket: Path,
    digest: str,
    python: Path,
    port: int,
    token: str,
    secret: bytes,
) -> Path:
    core_home = instance_service_home(identity, home=home)
    core_home.mkdir(parents=True, mode=0o700)
    (core_home / "logs").mkdir(mode=0o700)
    (core_home / "data").mkdir(mode=0o700)
    binding = {
        "schema_version": 1,
        "bridge_id": identity.bridge_id,
        "core_id": identity.instance_id,
        "key_id": f"key-{identity.instance_id}",
        "secret_hex": secret.hex(),
        "workdir_alias": "Workspace",
        "workdir_path": str(workspace),
        "policy_revision": digest,
        "bridge_config_path": str(bridge_config),
        "socket_path": str(bridge_socket),
    }
    _private_file(
        core_home / "shared-bridge-binding.json",
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode(),
    )
    snapshot = {
        "SERVERFS_HTTP_API_KEY": token,
        "SERVERFS_HTTP_ALLOWED_HOSTS": f"core.example.test,127.0.0.1:{port}",
        "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
        "SERVERFS_DATA_HOME": str(core_home / "data"),
        "SERVERFS_FILE_INGRESS_ENABLED": "false",
    }
    _private_file(
        core_home / "http-origin-environment.json",
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(),
    )
    service = {
        "executable": str(python),
        "arguments": [
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
            identity.bridge_id,
        ],
        "working_directory": str(config.parent),
        "environment_file": str(core_home / "http-origin-environment.json"),
        "stdout_log": str(core_home / "logs/core.stdout.log"),
        "stderr_log": str(core_home / "logs/core.stderr.log"),
        "origin_owner": "independent-core",
    }
    document = {
        "schema_version": 1,
        "owner": "v0.25-named-shared-core",
        "instance_id": identity.instance_id,
        "bridge_mode": "shared",
        "bridge_id": identity.bridge_id,
        "core": {
            "config": str(config.resolve()),
            "sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "port": port,
            "hostname": "core.example.test",
        },
        "services": {"http-origin": service},
    }
    _private_json(core_home / "services.json", document)
    alias, verified_socket = resolve_shared_core_manifest(
        identity,
        config,
        port,
        home=home,
        environ={
            "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
            "SERVERFS_DATA_HOME": str(core_home / "data"),
            "SERVERFS_FILE_INGRESS_ENABLED": "false",
        },
        expected_executable=python,
    )
    assert alias == "Workspace" and verified_socket == bridge_socket
    return core_home


def _start_wheel_process(
    stack: ExitStack,
    *,
    python: Path,
    module: str,
    args: list[str],
    cwd: Path,
    home: Path,
    env_extra: dict[str, str],
    log: Path,
) -> subprocess.Popen[bytes]:
    stream = stack.enter_context(log.open("wb"))
    env = {"PATH": os.defpath, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(env_extra)
    child = subprocess.Popen(
        [str(python), "-I", "-B", "-m", module, *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stream,
        start_new_session=True,
    )
    stack.callback(_stop, child)
    return child


def test_installed_wheel_three_process_http_acceptance() -> None:
    scratch = Path(os.environ["SERVERFS_V0250_WHEEL_SCRATCH"]).resolve(strict=True)
    wheelhouse = Path(os.environ["SERVERFS_V0250_WHEELHOUSE"]).resolve(strict=True)
    assert scratch.is_relative_to(_ROOT / "deployment/macos")
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
    core_python = wheelhouse / "core-venv/bin/python"
    bridge_python = wheelhouse / "bridge-venv/bin/python"
    assert core_python.is_file() and bridge_python.is_file() and core_python != bridge_python
    core_version = _wheel_provenance(core_python, "serverfs_mcp.cli", "serverfs-mcp", scratch)
    bridge_version = _wheel_provenance(
        bridge_python, "serverfs_agent_bridge.main", "serverfs-agent-bridge", scratch
    )
    print(f"core_wheel={core_version} bridge_wheel={bridge_version}")
    hold_seconds = int(os.environ.get("SERVERFS_V0250_CF_HOLD_SECONDS", "0"))
    assert 0 <= hold_seconds <= 180, "public hold must be between 1 and 180 seconds"

    root = scratch / "case"
    socket_path = root / "owner.sock"
    _require_short_socket_path(socket_path)
    root.mkdir(mode=0o700)
    home = root / "home"
    home.mkdir(mode=0o700)
    instances = home / "Library/Application Support/ServerFS/instances"
    instances.mkdir(parents=True, mode=0o700)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    bridge_id = "wheel-owner"
    bridge_config = root / "bridge.json"
    _private_json(
        bridge_config,
        {
            "socket_path": str(socket_path),
            "protocol_mode": "v2",
            "bridge_id": bridge_id,
            "state_dir": str(root / "bridge-state"),
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
    bridge_cfg = BridgeConfig.load(bridge_config)
    digest = core_workdir_policy_digest(bridge_cfg.policies.get("Workspace"))
    identities = (
        InstanceIdentity("wheel-core-a", "shared", bridge_id),
        InstanceIdentity("wheel-core-b", "shared", bridge_id),
    )
    grant_secrets = (secrets.token_bytes(32), secrets.token_bytes(32))
    provision_private_core_grants(
        store=TaskStore(root / "bridge-state"),
        bridge_id=bridge_id,
        policies=bridge_cfg.policies,
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
                for identity, secret in zip(identities, grant_secrets, strict=True)
            ],
        },
    )
    tokens = (secrets.token_urlsafe(32), secrets.token_urlsafe(32))
    ports = (18765, _port()) if hold_seconds else (_port(), _port())
    if hold_seconds:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", 18765))
            except OSError as exc:
                raise AssertionError("public hold port 127.0.0.1:18765 is occupied") from exc
    while ports[0] == ports[1]:
        ports = (_port(), _port())

    async def exercise() -> None:
        with ExitStack() as stack:
            owner = _start_wheel_process(
                stack,
                python=bridge_python,
                module="serverfs_agent_bridge.main",
                args=["--config", str(bridge_config)],
                cwd=root,
                home=home,
                env_extra={},
                log=root / "bridge.stderr",
            )
            bridge_probe = CoreBridgeClientV2(
                socket_path,
                bridge_id=bridge_id,
                core_id=identities[0].instance_id,
                key_id=f"key-{identities[0].instance_id}",
                secret=grant_secrets[0],
                workdir_alias="Workspace",
                policy_revision=digest,
                timeout_seconds=4,
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if owner.poll() is not None:
                    raise AssertionError(
                        f"installed-wheel Bridge exited {owner.returncode}: "
                        f"{(root / 'bridge.stderr').read_text(errors='replace')}"
                    )
                try:
                    if await bridge_probe.call("bridge.ping", {}) == {"status": "ok"}:
                        break
                except Exception:
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError(
                    "installed-wheel Bridge readiness timeout: "
                    + (root / "bridge.stderr").read_text(errors="replace")[-1500:]
                )
            assert socket_path.is_socket()
            core_processes = []
            for index, identity in enumerate(identities):
                config = root / f"{identity.instance_id}.toml"
                config.write_text(
                    "[agent]\nenabled = true\n[agent.codex]\nenabled = true\n"
                    "[[workdirs]]\nalias = 'Workspace'\n"
                    f"path = '{workspace}'\nread_only = false\n"
                    "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n",
                    encoding="utf-8",
                )
                core_home = _prepare_core(
                    root=root,
                    home=home,
                    identity=identity,
                    config=config,
                    workspace=workspace,
                    bridge_config=bridge_config,
                    bridge_socket=socket_path,
                    digest=digest,
                    python=core_python,
                    port=ports[index],
                    token=tokens[index],
                    secret=grant_secrets[index],
                )
                process = _start_wheel_process(
                    stack,
                    python=core_python,
                    module="serverfs_mcp.cli",
                    args=[
                        "serve",
                        "--transport",
                        "streamable-http",
                        "--config",
                        str(config),
                        "--http-port",
                        str(ports[index]),
                        "--macos-instance",
                        identity.instance_id,
                        "--macos-shared-bridge",
                        bridge_id,
                    ],
                    cwd=root,
                    home=home,
                    env_extra={
                        "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
                        "SERVERFS_DATA_HOME": str(core_home / "data"),
                        "SERVERFS_FILE_INGRESS_ENABLED": "false",
                        "SERVERFS_HTTP_API_KEY": tokens[index],
                        "SERVERFS_HTTP_ALLOWED_HOSTS": (
                            f"core.example.test,127.0.0.1:{ports[index]}"
                        ),
                    },
                    log=root / f"{identity.instance_id}.stderr",
                )
                core_processes.append(process)

            try:
                for index, process in enumerate(core_processes):
                    log = root / f"{identities[index].instance_id}.stderr"
                    await _wait_for_core(process, ports[index], log)
                    assert await asyncio.to_thread(_http_status, ports[index]) == 401
                    assert (
                        await asyncio.to_thread(
                            _http_status, ports[index], token="wrong-wheel-token"
                        )
                        == 401
                    )
                    listing = await _mcp_call(ports[index], tokens[index], "list_workdirs", {})
                    assert {item["alias"] for item in listing.structured_content["workdirs"]} == {
                        "Workspace"
                    }

                submitted = await bridge_probe.call(
                    "task.submit",
                    {
                        "runtime": "fake",
                        "path": ".",
                        "profile": "review",
                        "prompt": "installed wheel isolation",
                    },
                )
                task_id = submitted["task_id"]
                cross = await _mcp_call(ports[1], tokens[1], "get_agent_task", {"task_id": task_id})
                assert cross.is_error, cross

                _stop(owner)
                assert owner.poll() is not None, "installed-wheel Bridge owner did not stop"
                socket_deadline = time.monotonic() + 5
                while socket_path.exists() and time.monotonic() < socket_deadline:
                    await asyncio.sleep(0.05)
                assert not socket_path.exists(), (
                    "gracefully stopped Bridge owner left its test-owned Unix socket behind: "
                    + (root / "bridge.stderr").read_text(errors="replace")[-1500:]
                )
                for index, process in enumerate(core_processes):
                    assert process.poll() is None, (
                        f"Core {identities[index].instance_id} exited while the independent "
                        "Bridge owner was stopped: "
                        + (root / f"{identities[index].instance_id}.stderr").read_text(
                            errors="replace"
                        )[-1500:]
                    )
                    listing = await _mcp_call(ports[index], tokens[index], "list_workdirs", {})
                    assert {item["alias"] for item in listing.structured_content["workdirs"]} == {
                        "Workspace"
                    }

                restarted_owner = _start_wheel_process(
                    stack,
                    python=bridge_python,
                    module="serverfs_agent_bridge.main",
                    args=["--config", str(bridge_config)],
                    cwd=root,
                    home=home,
                    env_extra={},
                    log=root / "bridge-restart.stderr",
                )
                restart_deadline = time.monotonic() + 15
                while time.monotonic() < restart_deadline:
                    if restarted_owner.poll() is not None:
                        restart_log = (root / "bridge-restart.stderr").read_text(errors="replace")[
                            -1500:
                        ]
                        raise AssertionError(
                            "restarted installed-wheel Bridge exited "
                            f"{restarted_owner.returncode}: {restart_log}"
                        )
                    try:
                        if await bridge_probe.call("bridge.ping", {}) == {"status": "ok"}:
                            break
                    except Exception:
                        await asyncio.sleep(0.1)
                else:
                    raise AssertionError(
                        "restarted installed-wheel Bridge readiness timeout: "
                        + (root / "bridge-restart.stderr").read_text(errors="replace")[-1500:]
                    )

                for index, process in enumerate(core_processes):
                    core_log = (root / f"{identities[index].instance_id}.stderr").read_text(
                        errors="replace"
                    )[-1500:]
                    assert process.poll() is None, (
                        f"Core {identities[index].instance_id} exited during Bridge recovery: "
                        f"{core_log}"
                    )
                    listing = await _mcp_call(ports[index], tokens[index], "list_workdirs", {})
                    assert {item["alias"] for item in listing.structured_content["workdirs"]} == {
                        "Workspace"
                    }
                cross_after_restart = await _mcp_call(
                    ports[1], tokens[1], "get_agent_task", {"task_id": task_id}
                )
                assert cross_after_restart.is_error, cross_after_restart
                if hold_seconds:
                    _private_file(root / "public-bearer", tokens[0].encode())
                    ready = {
                        "port": 18765,
                        "pids": {
                            "bridge": restarted_owner.pid,
                            "core_a": core_processes[0].pid,
                            "core_b": core_processes[1].pid,
                        },
                        "hostname": "macbook-v0250-serverfs.ntlx.top",
                        "ready_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    _private_json(root / "public-ready.json", ready)
                    print(f"PUBLIC_READY scratch={root} port=18765", flush=True)
                    deadline = time.monotonic() + hold_seconds
                    while time.monotonic() < deadline and not (root / "public-stop").exists():
                        await asyncio.sleep(min(0.25, deadline - time.monotonic()))
                _stop(core_processes[0])
                assert core_processes[0].poll() is not None
                assert restarted_owner.poll() is None and core_processes[1].poll() is None
                assert await bridge_probe.call("bridge.ping", {}) == {"status": "ok"}
            finally:
                for process in reversed(core_processes):
                    _stop(process)
                _stop(owner)

    asyncio.run(exercise())
    assert not socket_path.exists(), "owner Bridge left its test-owned Unix socket behind"
