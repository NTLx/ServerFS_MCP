"""Diagnose signed macOS owned Bridge Launcher with optional fake Connectors.

No launchd registration, no Core HTTP serving, no external network, no Agent
tasks. Creates and signs a random owned app, stages two synthetic Connectors,
starts only the signed Bridge ServiceLauncher directly, inspects private logs
when it cannot expose its own IPC, then shuts down and scopes cleanup.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from v0250_owned_core_direct_runtime_acceptance import (
    PROTECTED,
    shutdown,
    start_bridge,
)
from v0250_owned_smappservice_live_acceptance import _no_owned_children

from serverfs_mcp import darwin_instance_runtime as runtime
from serverfs_mcp.darwin_instance_bridge_identity import named_owned_bridge_resources
from serverfs_mcp.darwin_instance_owned_runtime import stage_owned_core_bridge
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

REPO = Path(__file__).resolve().parents[2]
BRIDGE_PYTHON = REPO / "agent_bridge/.venv/bin/python"
BUILD = REPO / "native/macos/service_manager/build.sh"


def _redacted(text: str, replacements: list[str]) -> str:
    for item in replacements:
        if item:
            text = text.replace(item, "[private]")
    return text[-1800:]


def _log(path: Path, replacements: list[str]) -> str:
    if not path.is_file() or path.is_symlink():
        return ""
    return _redacted(path.read_text(errors="replace"), replacements)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print("NOT_EXECUTED: explicit --execute required")
        return 2
    if sys.platform != "darwin":
        raise RuntimeError("requires macOS")
    baseline = tuple(runtime.darwin_service._launchd_job_pid(label) for label in PROTECTED)
    if any(pid is None for pid in baseline):
        raise RuntimeError("production baseline unavailable")

    name = "v25-diagnose-" + secrets.token_hex(4)
    identity = InstanceIdentity(instance_id=name, bridge_mode="owned", bridge_id=name)
    root = instance_service_home(identity)
    owner = root.parent
    resource = named_owned_bridge_resources(identity)
    if (
        owner.exists()
        or owner.is_symlink()
        or resource.socket_path.parent.exists()
        or resource.socket_path.parent.is_symlink()
    ):
        raise RuntimeError("test instance or Bridge socket namespace already occupied")
    workspace_temp = Path(tempfile.mkdtemp(prefix="serverfs-v25-owned-diag-")).resolve()
    errors: list[str] = []
    excerpts: dict[str, str] = {}
    ready = False
    staged = False
    cleared = False
    process = None
    secret_values: list[str] = []
    try:
        workdir = workspace_temp / "workdir"
        workdir.mkdir(mode=0o700)
        private = workspace_temp / "secrets"
        private.mkdir(mode=0o700)
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        key = "synthetic-" + secrets.token_hex(16)
        secret_values.append(key)
        config = workspace_temp / "serverfs.toml"
        config.write_text(
            "[agent]\nenabled=true\n[agent.codex]\nenabled=true\n"
            "[[workdirs]]\nalias='synthetic'\n"
            f"path='{workdir}'\nread_only=false\n"
            "agent_mode='workspace-write'\nagent_runtimes=['codex']\n"
        )
        env = private / "http.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{port}\n"
        )
        env.chmod(0o600)
        token = private / "cf-token"
        token.write_text("synthetic-cf-" + secrets.token_hex(10))
        token.chmod(0o600)
        control = private / "oa-control"
        control.write_text("synthetic-oa-" + secrets.token_hex(10))
        control.chmod(0o600)
        bearer = private / "oa-bearer"
        bearer.write_text("Bearer " + key)
        bearer.chmod(0o600)
        secret_values.extend([token.read_text(), control.read_text(), bearer.read_text()])
        fake = private / "fake-connector"
        fake.write_text("#!/bin/sh\nexec /bin/sleep 300\n")
        fake.chmod(0o700)
        output = workspace_temp / "build"
        subprocess.run(
            ["bash", str(BUILD), str(output)],
            check=True,
            capture_output=True,
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
        staged = True
        process = start_bridge(
            identity,
            resource,
            workspace_temp / "outer-bridge.stderr.log",
            via_launcher=True,
        )
        ready = True
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    finally:
        shutdown(process)
        excerpts = {
            "launcher": _log(
                workspace_temp / "outer-bridge.stderr.log",
                [str(workspace_temp), str(owner), *secret_values],
            ),
            "service": _log(
                root / "logs/bridge.stderr.log",
                [str(workspace_temp), str(owner), *secret_values],
            ),
        }
        if resource.socket_path.parent.is_dir() and not resource.socket_path.parent.is_symlink():
            try:
                resource.socket_path.parent.rmdir()
            except OSError:
                errors.append("named socket directory not empty")
        no_children = _no_owned_children(identity, resource.config_path)
        production_same = (
            tuple(runtime.darwin_service._launchd_job_pid(label) for label in PROTECTED) == baseline
        )
        if (
            no_children
            and production_same
            and not resource.socket_path.parent.exists()
            and (process is None or process.poll() is not None)
            and owner.name == name
            and owner.parent.name == "instances"
        ):
            try:
                if owner.exists() and not owner.is_symlink():
                    shutil.rmtree(owner)
                cleared = not owner.exists()
            except OSError:
                errors.append("test instance cleanup failed; retain evidence")
        else:
            errors.append("named process or filesystem ownership uncertain; retain evidence")
        if cleared:
            shutil.rmtree(workspace_temp)
    prod_same = (
        tuple(runtime.darwin_service._launchd_job_pid(label) for label in PROTECTED) == baseline
    )
    if not prod_same:
        errors.append("production PID baseline changed")
    output = {
        "result": "PASS" if ready and cleared and prod_same and not errors else "NOT_ACCEPTED",
        "signed_bridge_launcher_ready": ready,
        "synthetic_connectors_staged": staged,
        "launchd_register_called": False,
        "test_instance_removed": cleared,
        "production_pids_unchanged": prod_same,
        "launcher_stderr": excerpts["launcher"],
        "bridge_stderr": excerpts["service"],
        "retained_diagnostic_dir": None if cleared else str(workspace_temp),
        "retained_instance": None if cleared else name,
        "errors": errors,
    }
    print(json.dumps(output, sort_keys=True))
    return 0 if output["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
