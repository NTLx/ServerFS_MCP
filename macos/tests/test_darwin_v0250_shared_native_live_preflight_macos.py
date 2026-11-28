"""Opt-in native signed two-Core owner authentication WITHOUT SMAppService registration.

Foundation does not honor a process HOME override for its App Service bundle
lookup, so this uses two random, signed, provably unregistered candidates under
the current user's existing ServerFS instance root. It NEVER registers,
starts, stops or unregisters any macOS Login Item or service. The actual Bridge
owner and its grants are disposable local test subprocess/data under /tmp.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

import pytest

from serverfs_mcp import darwin_service
from serverfs_mcp.core_bridge_client_v2 import CoreBridgeClientV2
from serverfs_mcp.darwin_instance_shared_lifecycle import (
    DarwinSharedLifecycleError,
    preflight_shared_core,
)
from serverfs_mcp.darwin_instance_shared_unstage import unstage_shared_core
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.darwin_shared_runtime import create_shared_core_runtime
from serverfs_mcp.instance_identity import InstanceIdentity
from test_darwin_v0250_shared_owner_process_macos import _start_owner, _stop, _wait_owner
from test_darwin_v0250_shared_smappservice_live_macos import (
    _production_pids,
    _signed_preflight,
)
from test_darwin_v0250_shared_two_core_http_macos import (
    _port,
    _prepare_core,
    _private_json,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin"
    or os.environ.get("SERVERFS_ENABLE_SHARED_SIGNED_LIVE_PREFLIGHT_TEST") != "1",
    reason="requires explicit signed native two-Core non-registration preflight opt-in",
)


def _invoke_shared_preflight_cli(
    identity: InstanceIdentity, config: Path, port: int, source: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke the actual operator CLI in an isolated subprocess, never register."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SERVERFS_", "CONTROL_PLANE_", "PYTHON"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "serverfs_mcp.darwin_instance_cli",
            "--instance",
            identity.instance_id,
            "shared-preflight",
            "--bridge-id",
            str(identity.bridge_id),
            "--config",
            str(config),
            "--http-port",
            str(port),
        ],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _assert_signed_preflight_rejects_mutation(
    identity: InstanceIdentity,
    path: Path,
    mutated_bytes: bytes,
    protected_pids: tuple[int | None, ...],
    forbidden_output: tuple[str, ...] = (),
) -> None:
    """Reject one test-owned metadata mutation and restore its exact file state."""
    app = instance_service_home(identity) / APP_NAME
    launcher = app / "Contents/Resources/serverfs-service-launcher"
    manager = app / "Contents/MacOS/serverfs-service-manager"
    log_dir = instance_service_home(identity) / "logs"
    original_bytes = path.read_bytes()
    original_mode = stat.S_IMODE(path.stat().st_mode)
    original_logs = {item.name for item in log_dir.iterdir()}
    try:
        path.write_bytes(mutated_bytes)
        path.chmod(original_mode)
        for command in (
            [str(launcher), "--validate-only", identity.instance_id, "http-origin"],
            [str(manager), "register-preflight", "http-origin"],
        ):
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert result.returncode != 0, f"signed preflight accepted mutation of {path.name}"
            try:
                response = json.loads(result.stdout)
            except json.JSONDecodeError:
                response = None
            if isinstance(response, dict):
                assert response.get("ok") is not True
                assert response.get("eligible") is not True
            output = result.stdout + result.stderr
            assert str(path) not in output
            assert all(value not in output for value in forbidden_output)
            assert darwin_service._launchd_job_pid(identity.macos_label("core")) is None
            assert {item.name for item in log_dir.iterdir()} == original_logs
            assert _production_pids() == protected_pids
    finally:
        path.write_bytes(original_bytes)
        path.chmod(original_mode)
    assert path.read_bytes() == original_bytes
    assert stat.S_IMODE(path.stat().st_mode) == original_mode


def test_two_signed_shared_cores_preflight_against_one_live_owner_without_registration() -> None:
    source = Path(__file__).resolve().parents[1]
    before = _production_pids()
    assert all(pid is not None for pid in before), "production PID baseline missing"
    suffix = secrets.token_hex(5)
    identities = (
        InstanceIdentity(f"shpf-a-{suffix}", "shared", "shpf-owner"),
        InstanceIdentity(f"shpf-b-{suffix}", "shared", "shpf-owner"),
    )
    owners = [instance_service_home(identity).parent for identity in identities]
    assert all(not path.exists() and not path.is_symlink() for path in owners)
    scratch = Path(tempfile.mkdtemp(prefix=f"shpf-{suffix}-", dir="/tmp")).resolve(strict=True)
    scratch.chmod(0o700)
    verified_staged: set[str] = set()
    completed = False
    cleanup_errors: list[str] = []
    try:
        workspace = scratch / "workspace"
        workspace.mkdir(mode=0o700)
        private_home = scratch / "owner-home"
        private_home.mkdir(mode=0o700)
        runtime_parent = scratch / "runtime-parent"
        runtime_parent.mkdir(mode=0o700)
        uv = shutil.which("uv")
        assert uv is not None, "offline uv is required for this explicitly opt-in test"
        installed = create_shared_core_runtime(
            runtime_parent / "standalone-core",
            source,
            Path(sys.executable),
            Path(uv).resolve(strict=True),
        )
        python = Path(installed["python"])
        template_root = scratch / "native-build"
        built = subprocess.run(
            ["bash", str(source / "native/macos/service_manager/build.sh"), str(template_root)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert built.returncode == 0, "isolated signed native bundle build failed"
        template = template_root / APP_NAME

        owner_socket = scratch / "bridge.sock"
        owner_config = scratch / "bridge.json"
        owner_state = scratch / "bridge-state"
        _private_json(
            owner_config,
            {
                "protocol_mode": "v2",
                "bridge_id": "shpf-owner",
                "socket_path": str(owner_socket),
                "state_dir": str(owner_state),
                "lock_dir": str(scratch / "bridge-locks"),
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
        from serverfs_agent_bridge.config import BridgeConfig
        from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
        from serverfs_agent_bridge.core_grants_private import provision_private_core_grants
        from serverfs_agent_bridge.store import TaskStore

        config = BridgeConfig.load(owner_config)
        digest = core_workdir_policy_digest(config.policies.get("Workspace"))
        keys = (secrets.token_bytes(32), secrets.token_bytes(32))
        grants = [
            {
                "core_id": identity.instance_id,
                "key_id": f"key-{identity.instance_id}",
                "secret_hex": key.hex(),
                "methods": ["bridge.ping", "task.submit", "task.get", "task.events"],
                "workdirs": [{"alias": "Workspace", "policy_sha256": digest}],
            }
            for identity, key in zip(identities, keys, strict=True)
        ]

        def install_grants(grants_to_install: list[dict]) -> None:
            provision_private_core_grants(
                store=TaskStore(owner_state),
                bridge_id="shpf-owner",
                policies=config.policies,
                document={
                    "schema_version": 1,
                    "bridge_id": "shpf-owner",
                    "grants": grants_to_install,
                },
            )

        install_grants(grants)
        ports = (_port(), _port())
        while ports[0] == ports[1]:
            ports = (_port(), _port())
        tokens = (secrets.token_hex(18), secrets.token_hex(18))
        direct = [
            CoreBridgeClientV2(
                owner_socket,
                bridge_id="shpf-owner",
                core_id=identity.instance_id,
                key_id=f"key-{identity.instance_id}",
                secret=key,
                workdir_alias="Workspace",
                policy_revision=digest,
                timeout_seconds=4,
            )
            for identity, key in zip(identities, keys, strict=True)
        ]

        async def exercise() -> None:
            with ExitStack() as stack:
                owner, owner_log = _start_owner(
                    stack, source, scratch, private_home, owner_config, 1
                )
                await _wait_owner(owner, owner_log, direct[0])

                staged_configs: list[Path] = []
                for i, identity in enumerate(identities):
                    core_config, _ = _prepare_core(
                        root=scratch,
                        home=Path.home(),
                        identity=identity,
                        bridge_config=owner_config,
                        bridge_socket=owner_socket,
                        policy_digest=digest,
                        workspace=workspace,
                        template_app=template,
                        python=python,
                        port=ports[i],
                        token=tokens[i],
                        secret=keys[i],
                    )
                    verified_staged.add(identity.instance_id)
                    staged_configs.append(core_config)
                    service_home = instance_service_home(identity)
                    manifest_path = service_home / "services.json"
                    environment_path = service_home / "http-origin-environment.json"

                    _assert_signed_preflight_rejects_mutation(
                        identity,
                        core_config,
                        core_config.read_bytes() + b"\n# signed preflight tamper\n",
                        before,
                    )
                    environment = json.loads(environment_path.read_text(encoding="utf-8"))
                    environment["SERVERFS_HTTP_ALLOWED_HOSTS"] += ",tampered.example.test"
                    _assert_signed_preflight_rejects_mutation(
                        identity,
                        environment_path,
                        json.dumps(environment, sort_keys=True, separators=(",", ":")).encode(),
                        before,
                        ("tampered.example.test",),
                    )
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["owner"] = "tampered-shared-core"
                    _assert_signed_preflight_rejects_mutation(
                        identity,
                        manifest_path,
                        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
                        before,
                        ("tampered-shared-core",),
                    )
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    invalid_python = f"/tmp/serverfs-shpf-missing-python-{identity.instance_id}"
                    manifest["services"]["http-origin"]["executable"] = invalid_python
                    _assert_signed_preflight_rejects_mutation(
                        identity,
                        manifest_path,
                        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
                        before,
                        (invalid_python,),
                    )

                    _signed_preflight(identity)
                    assert list((service_home / "logs").iterdir()) == []
                    assert darwin_service._launchd_job_pid(identity.macos_label("core")) is None
                    assert _production_pids() == before
                    # This is the public product preflight, including real
                    # signed App path and live Bridge v2 authorization.
                    result = await asyncio.to_thread(
                        preflight_shared_core, identity, core_config, ports[i]
                    )
                    assert result["state"] == "ready-to-register"
                    assert result["bridge_authenticated"] is True
                    assert result["core_registration"] == "not-registered"
                    cli = await asyncio.to_thread(
                        _invoke_shared_preflight_cli, identity, core_config, ports[i], source
                    )
                    assert cli.returncode == 0, "real shared-preflight CLI unexpectedly refused"
                    assert json.loads(cli.stdout)["state"] == "ready-to-register"
                    assert darwin_service._launchd_job_pid(identity.macos_label("core")) is None
                    assert _production_pids() == before

                assert owner.poll() is None
                # Installer-managed Python provenance is also part of the
                # operator's signed Shared Core preflight. A mode-drifted
                # record must refuse BOTH Cores before querying native status.
                provenance = python.parent.parent / "runtime-provenance.json"
                assert stat.S_IMODE(provenance.stat().st_mode) == 0o600
                provenance.chmod(0o644)
                try:
                    for i, identity in enumerate(identities):
                        with pytest.raises(DarwinSharedLifecycleError):
                            await asyncio.to_thread(
                                preflight_shared_core, identity, staged_configs[i], ports[i]
                            )
                finally:
                    provenance.chmod(0o600)
                for i, identity in enumerate(identities):
                    verified = await asyncio.to_thread(
                        preflight_shared_core, identity, staged_configs[i], ports[i]
                    )
                    assert verified["bridge_authenticated"] is True
                assert _production_pids() == before

                # An unavailable Bridge must refuse BOTH signed Cores before
                # registration, without stopping or changing those Cores.
                _stop(owner)
                for i, identity in enumerate(identities):
                    with pytest.raises(DarwinSharedLifecycleError):
                        await asyncio.to_thread(
                            preflight_shared_core, identity, staged_configs[i], ports[i]
                        )

                # Restart the SAME synthetic owner; both Cores authenticate
                # without re-staging or changing signed app metadata.
                owner2, log2 = _start_owner(stack, source, scratch, private_home, owner_config, 2)
                await _wait_owner(owner2, log2, direct[0])
                for i, identity in enumerate(identities):
                    assert (
                        await asyncio.to_thread(
                            preflight_shared_core, identity, staged_configs[i], ports[i]
                        )
                    )["bridge_authenticated"] is True

                # Owner-only revoke B, prove A can still pass but B fails.
                _stop(owner2)
                install_grants([grants[0]])
                owner3, log3 = _start_owner(stack, source, scratch, private_home, owner_config, 3)
                await _wait_owner(owner3, log3, direct[0])
                assert (
                    await asyncio.to_thread(
                        preflight_shared_core, identities[0], staged_configs[0], ports[0]
                    )
                )["bridge_authenticated"] is True
                with pytest.raises(DarwinSharedLifecycleError):
                    await asyncio.to_thread(
                        preflight_shared_core, identities[1], staged_configs[1], ports[1]
                    )
                denied_cli = await asyncio.to_thread(
                    _invoke_shared_preflight_cli,
                    identities[1],
                    staged_configs[1],
                    ports[1],
                    source,
                )
                assert denied_cli.returncode == 2
                assert "shared Core preflight failed" in denied_cli.stderr
                assert keys[1].hex() not in denied_cli.stderr
                assert owner3.poll() is None
                assert _production_pids() == before

        asyncio.run(exercise())
        completed = True
    finally:
        # No registration was ATTEMPTED. Only the ordinary conservative
        # unstage API may remove an intact, never-started candidate.
        for identity in reversed(identities):
            root = instance_service_home(identity)
            if root.is_symlink():
                cleanup_errors.append(f"{identity.instance_id}: unexpected symlink retained")
                continue
            if root.exists():
                if identity.instance_id not in verified_staged:
                    cleanup_errors.append(f"{identity.instance_id}: unverified stage retained")
                    continue
                try:
                    unstage_shared_core(identity)
                except Exception as exc:
                    cleanup_errors.append(f"{identity.instance_id}: {type(exc).__name__}")
            owner_path = root.parent
            if owner_path.exists() or owner_path.is_symlink():
                cleanup_errors.append(f"{identity.instance_id}: test root retained")
            if darwin_service._launchd_job_pid(identity.macos_label("core")) is not None:
                cleanup_errors.append(f"{identity.instance_id}: unexpected launchd PID")
        if _production_pids() != before:
            cleanup_errors.append("production PID baseline changed")
        if not cleanup_errors:
            shutil.rmtree(scratch)
        else:
            cleanup_errors.append("scratch retained for diagnostics")
        if cleanup_errors:
            pytest.fail("; ".join(cleanup_errors))
    assert completed
    assert not scratch.exists()
    assert all(not path.exists() for path in owners)
