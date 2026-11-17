"""macOS v0.25 dual independent no-network ingress on one file-only Core.

Explicit opt-in native ServiceManagement acceptance. Two fake connector
executables only exec /bin/sleep, never real tunnels or provider agents.
Tests one Core + independent Cloudflare and OpenAI labels, isolated stop/
restart and HTTP MCP continuity. This does NOT prove hosted ingress E2E.
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
    stage_cloudflare_connector,
    stage_file_only_instance,
    stage_openai_http_connector,
    start_component,
    stop_component,
    unstage_cloudflare_connector,
    unstage_file_only_instance,
    unstage_openai_http_connector,
)
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

SERVICES = {
    "core": "http-origin",
    "cloudflare": "cloudflare",
    "openai": "tunnel",
}
LOG_NAMES = {
    "core.stdout.log",
    "core.stderr.log",
    "cloudflare.stdout.log",
    "cloudflare.stderr.log",
    "openai.stdout.log",
    "openai.stderr.log",
}


def _state(identity: InstanceIdentity, component: str) -> str:
    return _manager(identity, "status", SERVICES[component])


def _verify_pids(identity: InstanceIdentity, expected: dict[str, int]) -> None:
    for name, pid in expected.items():
        if darwin_service._launchd_job_pid(identity.macos_label(name)) != pid:
            raise RuntimeError(f"named {name} PID changed unexpectedly")


def _delete_only_test_logs(root: Path) -> None:
    logs = root / "logs"
    if not logs.is_dir() or logs.is_symlink():
        raise RuntimeError("named test log directory is unsafe")
    for entry in logs.iterdir():
        st = entry.stat(follow_symlinks=False)
        if (
            entry.name not in LOG_NAMES
            or entry.is_symlink()
            or not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
        ):
            raise RuntimeError("unknown runtime log; test cleanup refused")
    for entry in logs.iterdir():
        entry.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print("NOT_EXECUTED: explicit --execute required")
        return 2

    before = _production_pids()
    if any(pid is None for pid in before.values()):
        raise RuntimeError("default production PID baseline incomplete")
    if not Path("/bin/sleep").is_file():
        raise RuntimeError("offline fixture executable missing")
    identity = InstanceIdentity(
        instance_id="v25-dual-" + secrets.token_hex(4), bridge_mode="disabled"
    )
    root = instance_service_home(identity)
    if root.exists() or root.is_symlink():
        raise RuntimeError("fresh named instance unexpectedly exists")
    temp = Path(tempfile.mkdtemp(prefix="serverfs-v25-dual-ingress-")).resolve()
    core_staged = False
    cf_staged = False
    oa_staged = False
    tested = False
    errors: list[str] = []
    mcp_checks = 0
    try:
        workdir = temp / "workdir"
        private = temp / "secrets"
        workdir.mkdir(mode=0o700)
        private.mkdir(mode=0o700)
        (workdir / "hello.txt").write_text("synthetic-file-only")
        config = temp / "serverfs.toml"
        config.write_text(f"[[workdirs]]\nalias='isolated'\npath='{workdir}'\nread_only=false\n")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        api_key = "synthetic-" + secrets.token_hex(12)
        env = private / "core.env"
        env.write_text(
            f"SERVERFS_HTTP_API_KEY={api_key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=dual.invalid,127.0.0.1:{port}\n"
        )
        env.chmod(0o600)
        token = private / "cf-token"
        token.write_text("synthetic-cloudflare-only")
        token.chmod(0o600)
        control = private / "openai-control"
        control.write_text("synthetic-no-network-control")
        control.chmod(0o600)
        bearer = private / "openai-bearer"
        bearer.write_text("Bearer " + api_key)
        bearer.chmod(0o600)
        connector = private / "no-network-connector"
        connector.write_text("#!/bin/sh\nexec /bin/sleep 300\n")
        connector.chmod(0o700)

        output = temp / "build"
        built = subprocess.run(
            [
                "bash",
                str(Path(__file__).resolve().parents[2] / "native/macos/service_manager/build.sh"),
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if built.returncode:
            raise RuntimeError("isolated signed App build failed")

        stage_file_only_instance(identity, config, env, port, "dual.invalid", output / APP_NAME)
        core_staged = True
        stage_cloudflare_connector(identity, connector, token)
        cf_staged = True
        stage_openai_http_connector(
            identity,
            connector,
            "tunnel_" + secrets.token_hex(16),
            control,
            bearer,
        )
        oa_staged = True

        core = start_component(identity, "core")["pid"]
        cloudflare = start_component(identity, "cloudflare")["pid"]
        openai = start_component(identity, "openai")["pid"]
        if len({core, cloudflare, openai}) != 3:
            raise RuntimeError("named components do not have separate PIDs")
        expected = {"core": core, "cloudflare": cloudflare, "openai": openai}
        _verify_pids(identity, expected)

        # The Core must never be stopped while either Connector is registered.
        try:
            stop_component(identity, "core")
        except DarwinInstanceRuntimeError as exc:
            if "Connector before" not in str(exc):
                raise
        else:
            raise RuntimeError("active Connector dependency was bypassed")
        url = f"http://127.0.0.1:{port}/mcp"
        asyncio.run(_mcp_roundtrip(url, api_key))
        mcp_checks += 1

        stop_component(identity, "cloudflare")
        _verify_pids(identity, {"core": core, "openai": openai})
        asyncio.run(_mcp_roundtrip(url, api_key))
        mcp_checks += 1
        cloudflare = start_component(identity, "cloudflare")["pid"]
        _verify_pids(identity, {"core": core, "openai": openai, "cloudflare": cloudflare})

        stop_component(identity, "openai")
        _verify_pids(identity, {"core": core, "cloudflare": cloudflare})
        asyncio.run(_mcp_roundtrip(url, api_key))
        mcp_checks += 1
        openai = start_component(identity, "openai")["pid"]
        _verify_pids(identity, {"core": core, "cloudflare": cloudflare, "openai": openai})
        asyncio.run(_mcp_roundtrip(url, api_key))
        mcp_checks += 1
        tested = True
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    finally:
        if core_staged:
            try:
                if oa_staged:
                    if _state(identity, "openai") not in {"not-found", "not-registered"}:
                        stop_component(identity, "openai")
                    unstage_openai_http_connector(identity)
                if cf_staged:
                    if _state(identity, "cloudflare") not in {"not-found", "not-registered"}:
                        stop_component(identity, "cloudflare")
                    unstage_cloudflare_connector(identity)
                if _state(identity, "core") not in {"not-found", "not-registered"}:
                    stop_component(identity, "core")
                for component in SERVICES:
                    if darwin_service._launchd_job_pid(identity.macos_label(component)) is not None:
                        raise RuntimeError("test-owned native job is still active")
                _delete_only_test_logs(root)
                unstage_file_only_instance(identity)
            except Exception as exc:
                errors.append("cleanup: " + type(exc).__name__ + ": " + str(exc))
        removed = not root.exists()
        untouched = _production_pids() == before
        if removed:
            shutil.rmtree(temp)
        final = {
            "result": "PASS" if tested and removed and untouched and not errors else "NOT_ACCEPTED",
            "network": "synthetic connectors only",
            "same_core_two_connector_lifecycle": tested,
            "mcp_file_roundtrips": mcp_checks,
            "test_instance_removed": removed,
            "production_pids_unchanged": untouched,
            "retained_instance": None if removed else identity.instance_id,
            "retained_diagnostic_dir": None if removed else str(temp),
            "errors": errors,
        }
        print(json.dumps(final, sort_keys=True))
    return 0 if final["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
