"""One opt-in macOS SMAppService named-owned Core+Bridge acceptance.

Registers ONLY a random, freshly staged, private signed instance, never the
four default jobs. Uses synthetic credentials, no provider tasks or egress.
Rolls back Core then Bridge and verifies native statuses/PIDs and IPC cleanup.
Never discards state if registration or process ownership remains uncertain.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx2
from v0250_owned_core_direct_runtime_acceptance import PROTECTED, probe_core

from serverfs_mcp.darwin_instance_bridge_identity import named_owned_bridge_resources
from serverfs_mcp.darwin_instance_owned_lifecycle import (
    start_owned_pair,
    stop_owned_pair,
)
from serverfs_mcp.darwin_instance_owned_runtime import stage_owned_core_bridge
from serverfs_mcp.darwin_instance_runtime import _manager, darwin_service
from serverfs_mcp.darwin_instances import APP_NAME, instance_app_path, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

REPO = Path(__file__).resolve().parents[2]
BRIDGE_PYTHON = REPO / "agent_bridge/.venv/bin/python"
BUILD = REPO / "native/macos/service_manager/build.sh"


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _native_preflight(identity: InstanceIdentity) -> None:
    app = instance_app_path(identity)
    manager = app / "Contents/MacOS/serverfs-service-manager"
    result = subprocess.run(
        [str(manager), "register-preflight", "agent-bridge"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    try:
        doc = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("native registration preflight response is invalid") from exc
    if (
        result.returncode != 0
        or doc.get("eligible") is not True
        or doc.get("instance_id") != identity.instance_id
    ):
        raise RuntimeError("native named Bridge registration preflight refused")


def _none_registered(identity: InstanceIdentity) -> bool:
    return all(
        _manager(identity, "status", component) in {"not-registered", "not-found"}
        and darwin_service._launchd_job_pid(
            identity.macos_label("bridge" if component == "agent-bridge" else "core")
        )
        is None
        for component in ("http-origin", "agent-bridge")
    )


def _no_owned_children(identity: InstanceIdentity, bridge_config: Path) -> bool:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return False
    # The signed launchers exec processes carrying these exact private
    # markers. An unknown matching process means ownership is not proven.
    return all(
        f"--macos-instance {identity.instance_id}" not in line
        and f"--instance {identity.instance_id}" not in line
        and str(bridge_config) not in line
        for line in result.stdout.splitlines()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("NOT_EXECUTED: explicit --execute is required")
        return 2
    if sys.platform != "darwin":
        raise RuntimeError("SMAppService acceptance requires macOS")
    baseline = tuple(darwin_service._launchd_job_pid(label) for label in PROTECTED)
    if any(pid is None for pid in baseline):
        raise RuntimeError("production launchd PID baseline incomplete")
    token = "v25-sm-" + secrets.token_hex(5)
    identity = InstanceIdentity(instance_id=token, bridge_mode="owned", bridge_id=token)
    root = instance_service_home(identity)
    owner = root.parent
    if owner.exists() or owner.is_symlink():
        raise RuntimeError("test owner already exists; no registration attempted")
    started = False
    registered = False
    cleaned = False
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-sm-native-") as temp:
        scratch = Path(temp).resolve(strict=True)
        port = _port()
        key = "synthetic-" + secrets.token_hex(16)
        try:
            workspace = scratch / "workdir"
            workspace.mkdir(mode=0o700)
            secrets_dir = scratch / "secrets"
            secrets_dir.mkdir(mode=0o700)
            config = scratch / "serverfs.toml"
            config.write_text(
                "[agent]\nenabled=true\n[agent.codex]\nenabled=true\n"
                "[[workdirs]]\nalias='synthetic'\n"
                f"path='{workspace}'\nread_only=false\n"
                "agent_mode='workspace-write'\nagent_runtimes=['codex']\n"
            )
            env_file = secrets_dir / "http.env"
            env_file.write_text(
                f"SERVERFS_HTTP_API_KEY={key}\n"
                f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{port}\n"
            )
            env_file.chmod(0o600)
            template = scratch / "template"
            subprocess.run(
                ["bash", str(BUILD), str(template)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            stage_owned_core_bridge(
                identity,
                config,
                env_file,
                port,
                "synthetic.test",
                template / APP_NAME,
                BRIDGE_PYTHON,
            )
            _native_preflight(identity)
            if not _none_registered(identity):
                raise RuntimeError("random test labels already registered")
            result = start_owned_pair(identity, config, port)
            registered = result["state"] == "enabled"
            if not registered:
                raise RuntimeError("owned pair was not enabled")
            url = f"http://127.0.0.1:{port}/mcp"
            with httpx2.Client(trust_env=False, timeout=2) as http:
                if http.get(url).status_code != 401:
                    raise RuntimeError("named Core anonymous authentication failed")
                if (
                    http.get(
                        url, headers={"Authorization": "Bearer intentionally-wrong"}
                    ).status_code
                    != 401
                ):
                    raise RuntimeError("named Core accepted foreign bearer credential")
            asyncio.run(probe_core(port, key))
            started = True
        except Exception as exc:
            # Do not expose private environment content or logs.
            errors.append(type(exc).__name__ + ": " + str(exc))
        finally:
            if owner.is_dir() and not owner.is_symlink():
                try:
                    stop_owned_pair(identity)
                except Exception as exc:
                    errors.append("scoped unregister failed: " + type(exc).__name__)

            # An application must never be deleted while registration,
            # launchd ownership or the supervised child is uncertain.
            resource = named_owned_bridge_resources(identity)
            sock_parent = resource.socket_path.parent
            if sock_parent.is_dir() and not sock_parent.is_symlink():
                try:
                    sock_parent.rmdir()
                except OSError:
                    errors.append("named Bridge socket namespace retained")
            try:
                inactive = _none_registered(identity) if owner.exists() else True
            except Exception:
                inactive = False
            no_children = _no_owned_children(identity, resource.config_path)
            if not inactive or not no_children or sock_parent.exists():
                errors.append("owned service state is not proven inactive; preserve instance")
            elif owner.is_dir() and not owner.is_symlink():
                if owner.name != token or owner.parent.name != "instances":
                    errors.append("synthetic owner cleanup identity mismatch")
                else:
                    try:
                        # Only this random test instance, absent before this
                        # function, can be discarded after native unregister
                        # and child-process proof. Never delete other state.
                        shutil.rmtree(owner)
                    except OSError:
                        errors.append("synthetic test owner cleanup refused")
            cleaned = not owner.exists()
    unchanged = tuple(darwin_service._launchd_job_pid(label) for label in PROTECTED) == baseline
    if not unchanged:
        errors.append("production launchd PID baseline changed")
    payload = {
        "result": "PASS" if started and cleaned and not errors else "NOT_ACCEPTED",
        "smappservice_pair_usable": started,
        "scope": "unique-named-owned",
        "test_instance_removed": cleaned,
        "production_pids_unchanged": unchanged,
        "errors": errors,
        "retained_test_instance": token if not cleaned else None,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
