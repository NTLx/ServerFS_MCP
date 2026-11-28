"""macOS v0.25 synthetic owned Core + Bridge + dual Connectors SMAppService E2E.

Explicit opt-in. New random owned instance, local loopback MCP only. Both
Cloudflare and OpenAI Connector executables are harmless wrappers around
/bin/sleep. No hosted ingress, real credentials or Agent task submission.
Any uncertain child/registration ownership retains its isolated evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from v0250_owned_core_direct_runtime_acceptance import PROTECTED, probe_core
from v0250_owned_smappservice_live_acceptance import (
    _native_preflight,
    _no_owned_children,
)

from serverfs_mcp import darwin_instance_runtime as runtime
from serverfs_mcp.darwin_instance_bridge_identity import named_owned_bridge_resources
from serverfs_mcp.darwin_instance_owned_lifecycle import (
    DarwinOwnedLifecycleError,
    owned_instance_status,
    start_owned_pair,
    stop_owned_pair,
)
from serverfs_mcp.darwin_instance_owned_runtime import stage_owned_core_bridge
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

REPO = Path(__file__).resolve().parents[2]
BRIDGE_PYTHON = REPO / "agent_bridge/.venv/bin/python"
BUILD = REPO / "native/macos/service_manager/build.sh"
SERVICES = {
    "bridge": "agent-bridge",
    "core": "http-origin",
    "cloudflare": "cloudflare",
    "openai": "tunnel",
}


def _port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _registrations_clear(identity: InstanceIdentity) -> bool:
    return all(
        runtime._manager(identity, "status", service) in {"not-registered", "not-found"}
        and runtime.darwin_service._launchd_job_pid(identity.macos_label(component)) is None
        for component, service in SERVICES.items()
    )


def _verify_running(identity: InstanceIdentity, expected: dict[str, int]) -> None:
    for component, pid in expected.items():
        if runtime.darwin_service._launchd_job_pid(identity.macos_label(component)) != pid:
            raise RuntimeError("named " + component + " process identity changed")


def _all_tracked_processes_exited(pids: set[int], *, timeout: float = 8.0) -> bool:
    """Do not mistake removed launchd labels for exited /bin/sleep processes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        still_alive = False
        for pid in pids:
            if type(pid) is not int or pid <= 0:
                return False
            try:
                os.kill(pid, 0)  # existence probe; sends no signal
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                return False
            else:
                still_alive = True
        if not still_alive:
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print("NOT_EXECUTED: explicit --execute required")
        return 2
    if sys.platform != "darwin":
        raise RuntimeError("real native macOS required")
    baseline = tuple(runtime.darwin_service._launchd_job_pid(label) for label in PROTECTED)
    if any(pid is None for pid in baseline):
        raise RuntimeError("production launchd PID baseline unavailable")
    instance_id = "v25-owned-dual-" + secrets.token_hex(4)
    identity = InstanceIdentity(instance_id=instance_id, bridge_mode="owned", bridge_id=instance_id)
    root = instance_service_home(identity)
    owner = root.parent
    if owner.exists() or owner.is_symlink():
        raise RuntimeError("new random instance name already exists")

    resource = named_owned_bridge_resources(identity)
    if resource.socket_path.parent.exists() or resource.socket_path.parent.is_symlink():
        raise RuntimeError("random owned Bridge socket namespace already exists")
    workspace_temp = Path(tempfile.mkdtemp(prefix="serverfs-v25-owned-dual-")).resolve()
    tested = False
    removed = False
    errors: list[str] = []
    roundtrips = 0
    tracked_pids: set[int] = set()
    try:
        workdir = workspace_temp / "workdir"
        workdir.mkdir(mode=0o700)
        private = workspace_temp / "secrets"
        private.mkdir(mode=0o700)
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        api_key = "synthetic-" + secrets.token_hex(16)
        config = workspace_temp / "serverfs.toml"
        config.write_text(
            "[agent]\nenabled = true\n[agent.codex]\nenabled = true\n"
            "[[workdirs]]\nalias='synthetic'\n"
            f"path='{workdir}'\nread_only=false\n"
            "agent_mode='workspace-write'\nagent_runtimes=['codex']\n"
        )
        env = private / "core.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={api_key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{port}\n"
        )
        env.chmod(0o600)
        token = private / "cloudflare-token"
        token.write_text("synthetic-test-token-not-cloudflare")
        token.chmod(0o600)
        control = private / "control-plane"
        control.write_text("synthetic-test-key-not-openai")
        control.chmod(0o600)
        bearer = private / "core-bearer"
        bearer.write_text("Bearer " + api_key)
        bearer.chmod(0o600)
        fake = private / "synthetic-connector"
        fake.write_text("#!/bin/sh\nexec /bin/sleep 300\n")
        fake.chmod(0o700)
        output = workspace_temp / "build"
        subprocess.run(
            ["bash", str(BUILD), str(output)],
            capture_output=True,
            check=True,
            timeout=120,
        )
        stage_owned_core_bridge(
            identity,
            config,
            env,
            port,
            "synthetic.test",
            output / APP_NAME,
            BRIDGE_PYTHON,
        )
        runtime.stage_cloudflare_connector(identity, fake, token)
        runtime.stage_openai_http_connector(
            identity,
            fake,
            "tunnel_" + secrets.token_hex(16),
            control,
            bearer,
        )
        _native_preflight(identity)
        if not _registrations_clear(identity):
            raise RuntimeError("new owned instance labels unexpectedly registered")
        pair = start_owned_pair(identity, config, port)
        bridge_pid = pair["bridge_pid"]
        core_pid = pair["core_pid"]
        tracked_pids.update((bridge_pid, core_pid))
        asyncio.run(probe_core(port, api_key))
        roundtrips += 1
        cf_pid = runtime.start_component(identity, "cloudflare")["pid"]
        tracked_pids.add(cf_pid)
        oa_pid = runtime.start_component(identity, "openai")["pid"]
        tracked_pids.add(oa_pid)
        if len({bridge_pid, core_pid, cf_pid, oa_pid}) != 4:
            raise RuntimeError("four named components do not own distinct PIDs")
        _verify_running(
            identity,
            {
                "bridge": bridge_pid,
                "core": core_pid,
                "cloudflare": cf_pid,
                "openai": oa_pid,
            },
        )
        status = owned_instance_status(identity)
        if (
            status["registrations"].get("cloudflare") != "enabled"
            or status["registrations"].get("openai") != "enabled"
        ):
            raise RuntimeError("owned status omitted Connector state")

        try:
            stop_owned_pair(identity)
        except DarwinOwnedLifecycleError as exc:
            if "Connector" not in str(exc):
                raise
        else:
            raise RuntimeError("owned pair stopped while two Connectors were active")

        runtime.stop_component(identity, "cloudflare")
        _verify_running(identity, {"bridge": bridge_pid, "core": core_pid, "openai": oa_pid})
        asyncio.run(probe_core(port, api_key))
        roundtrips += 1
        cf_pid = runtime.start_component(identity, "cloudflare")["pid"]
        tracked_pids.add(cf_pid)

        runtime.stop_component(identity, "openai")
        _verify_running(identity, {"bridge": bridge_pid, "core": core_pid, "cloudflare": cf_pid})
        asyncio.run(probe_core(port, api_key))
        roundtrips += 1
        oa_pid = runtime.start_component(identity, "openai")["pid"]
        tracked_pids.add(oa_pid)
        _verify_running(
            identity,
            {"bridge": bridge_pid, "core": core_pid, "cloudflare": cf_pid, "openai": oa_pid},
        )
        asyncio.run(probe_core(port, api_key))
        roundtrips += 1

        runtime.stop_component(identity, "cloudflare")
        runtime.stop_component(identity, "openai")
        stop_owned_pair(identity)
        tested = True
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    finally:
        if root.is_dir() and not root.is_symlink():
            # Reverse-order scoped rollback. No default label is passed here.
            for component in ("cloudflare", "openai"):
                try:
                    service = SERVICES[component]
                    if runtime._manager(identity, "status", service) not in {
                        "not-registered",
                        "not-found",
                    }:
                        runtime.stop_component(identity, component)
                except Exception as exc:
                    errors.append("Connector rollback " + component + ": " + type(exc).__name__)
            try:
                stop_owned_pair(identity)
            except Exception as exc:
                errors.append("owned pair rollback: " + type(exc).__name__)
        if resource.socket_path.parent.is_dir() and not resource.socket_path.parent.is_symlink():
            try:
                resource.socket_path.parent.rmdir()
            except OSError:
                errors.append("owned Bridge IPC namespace remains")
        try:
            inactive = _registrations_clear(identity) if owner.exists() else True
        except Exception:
            inactive = False
        children_gone = _no_owned_children(
            identity, resource.config_path
        ) and _all_tracked_processes_exited(tracked_pids)
        production_same = (
            tuple(runtime.darwin_service._launchd_job_pid(label) for label in PROTECTED) == baseline
        )
        if (
            not inactive
            or not children_gone
            or not production_same
            or resource.socket_path.parent.exists()
        ):
            errors.append("named service/child ownership uncertain; retain test instance")
        elif owner.is_dir() and not owner.is_symlink():
            if owner.parent.name != "instances" or owner.name != instance_id:
                errors.append("synthetic owner cleanup identity mismatch")
            else:
                try:
                    # Exact random test-only root, created absent at baseline.
                    shutil.rmtree(owner)
                except OSError:
                    errors.append("synthetic owner cleanup failed")
        removed = not owner.exists()
        if removed:
            shutil.rmtree(workspace_temp)
    unchanged = (
        tuple(runtime.darwin_service._launchd_job_pid(label) for label in PROTECTED) == baseline
    )
    if not unchanged:
        errors.append("default production launchd PID changed")
    outcome = {
        "result": "PASS" if tested and removed and unchanged and not errors else "NOT_ACCEPTED",
        "real_smappservice": tested,
        "fixture": "synthetic no-network /bin/sleep Connectors",
        "owned_core_bridge_connector_rpc": tested,
        "mcp_missing_task_roundtrips": roundtrips,
        "test_instance_removed": removed,
        "production_pids_unchanged": unchanged,
        "retained_instance": None if removed else instance_id,
        "retained_diagnostic_dir": None if removed else str(workspace_temp),
        "errors": errors,
    }
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
