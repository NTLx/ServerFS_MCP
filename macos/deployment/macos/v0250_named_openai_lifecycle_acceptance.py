"""macOS named file-only Core + synthetic OpenAI HTTP Connector native lifecycle.

An opt-in, no-network ServiceManagement acceptance. The executable merely
execs /bin/sleep and never launches the real OpenAI Tunnel client. Validates
staged private credential binding, independent start/stop/restart, Core MCP
file tools, and scoped cleanup. NOT a hosted OpenAI Tunnel E2E.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

from v0250_file_only_http_e2e import _mcp_roundtrip
from v0250_named_launchd_acceptance import _production_pids

from serverfs_mcp import darwin_service
from serverfs_mcp.darwin_instance_runtime import (
    DarwinInstanceRuntimeError,
    _manager,
    stage_file_only_instance,
    stage_openai_http_connector,
    start_component,
    stop_component,
    unstage_file_only_instance,
    unstage_openai_http_connector,
)
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity


def _remove_synthetic_logs(root: Path) -> None:
    logs = root / "logs"
    if not logs.is_dir() or logs.is_symlink():
        raise RuntimeError("synthetic log directory is untrusted")
    allowed = {
        "core.stdout.log",
        "core.stderr.log",
        "openai.stdout.log",
        "openai.stderr.log",
    }
    for item in logs.iterdir():
        data = item.stat(follow_symlinks=False)
        if (
            item.name not in allowed
            or item.is_symlink()
            or not stat.S_ISREG(data.st_mode)
            or data.st_uid != os.getuid()
        ):
            raise RuntimeError("synthetic log cleanup refused unknown artifact")
    for item in logs.iterdir():
        item.unlink()


def _state(identity: InstanceIdentity, name: str) -> str:
    return _manager(identity, "status", "tunnel" if name == "openai" else "http-origin")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print("NOT_EXECUTED: explicit --execute required")
        return 2

    baseline = _production_pids()
    if any(pid is None for pid in baseline.values()):
        raise RuntimeError("production launchd baseline incomplete")
    if not Path("/bin/sleep").is_file():
        raise RuntimeError("offline test process is unavailable")
    identity = InstanceIdentity(instance_id="v25o-" + secrets.token_hex(4), bridge_mode="disabled")
    root = instance_service_home(identity)
    if root.exists() or root.is_symlink():
        raise RuntimeError("random instance state collision")

    temporary = Path(tempfile.mkdtemp(prefix="serverfs-v25-openai-fake-")).resolve()
    errors: list[str] = []
    core_staged = False
    openai_staged = False
    stopped_independently = False
    credential_binding_verified = False
    try:
        workspace = temporary / "workdir"
        workspace.mkdir(mode=0o700)
        (workspace / "hello.txt").write_text("synthetic-file-only", encoding="utf-8")
        config = temporary / "core.toml"
        config.write_text(
            f"[[workdirs]]\nalias='isolated'\npath='{workspace}'\nread_only=false\n",
            encoding="utf-8",
        )
        private = temporary / "operator"
        private.mkdir(mode=0o700)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        key = "synthetic-" + secrets.token_hex(12)
        env = private / "core.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=smoke.invalid,127.0.0.1:{port}\n",
            encoding="utf-8",
        )
        env.chmod(0o600)
        control = private / "control"
        control.write_text("synthetic-control-plane-key", encoding="utf-8")
        control.chmod(0o600)
        bearer = private / "core-bearer"
        bearer.write_text("Bearer " + key, encoding="utf-8")
        bearer.chmod(0o600)
        fake_client = private / "no-network-tunnel-client"
        fake_client.write_text("#!/bin/sh\nexec /bin/sleep 300\n", encoding="utf-8")
        fake_client.chmod(0o700)
        build = temporary / "build"
        builder = Path(__file__).resolve().parents[2] / "native/macos/service_manager/build.sh"
        built = subprocess.run(
            ["bash", str(builder), str(build)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if built.returncode:
            raise RuntimeError("isolated signed macOS bundle build failed")
        stage_file_only_instance(identity, config, env, port, "smoke.invalid", build / APP_NAME)
        core_staged = True
        tunnel_id = "tunnel_" + secrets.token_hex(16)
        stage_openai_http_connector(identity, fake_client, tunnel_id, control, bearer)
        openai_staged = True
        manifest = (root / "services.json").read_text(encoding="utf-8")
        parsed = json.loads(manifest)
        spec = parsed["services"]["tunnel"]
        if (
            key in manifest
            or "synthetic-control-plane-key" in manifest
            or spec["control_key_file"] != str(control)
            or spec["core_bearer_file"] != str(bearer)
            or spec["tunnel_id"] != tunnel_id
            or spec["core_port"] != port
        ):
            raise RuntimeError("staged OpenAI Connector private credential binding failed")
        credential_binding_verified = True
        if _production_pids() != baseline:
            raise RuntimeError("production PID changed before registration")
        core_pid = start_component(identity, "core")["pid"]
        if not isinstance(core_pid, int):
            raise RuntimeError("named Core PID is missing")
        url = f"http://127.0.0.1:{port}/mcp"
        start_component(identity, "openai")
        if darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid:
            raise RuntimeError("OpenAI connector start affected Core")
        try:
            stop_component(identity, "core")
        except DarwinInstanceRuntimeError as exc:
            if "Connector before" not in str(exc):
                raise
        else:
            raise RuntimeError("Core stopped while OpenAI Connector was active")
        asyncio.run(_mcp_roundtrip(url, key))
        stop_component(identity, "openai")
        if darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid:
            raise RuntimeError("OpenAI Connector stop affected Core")
        start_component(identity, "openai")
        if darwin_service._launchd_job_pid(identity.macos_label("core")) != core_pid:
            raise RuntimeError("OpenAI Connector restart affected Core")
        asyncio.run(_mcp_roundtrip(url, key))
        stopped_independently = True
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    finally:
        if core_staged:
            try:
                if openai_staged:
                    if _state(identity, "openai") not in {"not-registered", "not-found"}:
                        stop_component(identity, "openai")
                    unstage_openai_http_connector(identity)
                if _state(identity, "core") not in {"not-registered", "not-found"}:
                    stop_component(identity, "core")
                for component in ("core", "openai"):
                    if darwin_service._launchd_job_pid(identity.macos_label(component)) is not None:
                        raise RuntimeError("named instance service still running")
                _remove_synthetic_logs(root)
                unstage_file_only_instance(identity)
            except Exception as exc:
                errors.append("cleanup: " + type(exc).__name__ + ": " + str(exc))
        clean = not root.exists()
        unchanged = _production_pids() == baseline
        if clean:
            shutil.rmtree(temporary)
        result = {
            "result": "PASS"
            if clean
            and unchanged
            and credential_binding_verified
            and stopped_independently
            and not errors
            else "NOT_ACCEPTED",
            "fixture": "synthetic no-network exec /bin/sleep",
            "private_credential_binding_verified": credential_binding_verified,
            "connector_stop_restart_preserves_core": stopped_independently,
            "mcp_roundtrips": 2 if stopped_independently else None,
            "named_instance_removed": clean,
            "production_pids_unchanged": unchanged,
            "retained_diagnostic_dir": None if clean else str(temporary),
            "retained_instance": None if clean else identity.instance_id,
            "errors": errors,
        }
        print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
