"""One real isolated macOS named owned Core -> Bridge runtime acceptance.

Explicit --execute only. No launchd registration, provider tasks, external
network or production mutation. Creates a cryptographically unique named
instance under ServerFS/instances, then removes only its own state.
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
import sys
import tempfile
import time
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from serverfs_mcp.darwin_instance_bridge_identity import named_owned_bridge_resources
from serverfs_mcp.darwin_instance_owned_runtime import stage_owned_core_bridge
from serverfs_mcp.darwin_instances import APP_NAME, instance_app_path, instance_service_home
from serverfs_mcp.darwin_service import _launchd_job_pid
from serverfs_mcp.instance_identity import InstanceIdentity

REPO = Path(__file__).resolve().parents[2]
BRIDGE_PYTHON = REPO / "agent_bridge/.venv/bin/python"
BUILD = REPO / "native/macos/service_manager/build.sh"
PROTECTED = (
    "com.ntlx.serverfs.tunnel",
    "com.ntlx.serverfs.bridge",
    "com.ntlx.serverfs.http-origin",
    "com.ntlx.serverfs.cloudflare",
)


def shutdown(process: subprocess.Popen | None, *, supervised: bool = False) -> None:
    if process is None:
        return
    if supervised and process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is None and not supervised:
        process.terminate()
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)


def start_bridge(
    identity: InstanceIdentity, resource, log: Path, *, via_launcher: bool = False
) -> subprocess.Popen:
    argv = (
        [
            str(instance_app_path(identity) / "Contents/Resources/serverfs-service-launcher"),
            identity.instance_id,
            "agent-bridge",
        ]
        if via_launcher
        else [
            sys.executable,
            "-B",
            "-m",
            "serverfs_mcp.darwin_owned_bridge_supervisor",
            "--instance",
            identity.instance_id,
        ]
    )
    with log.open("wb") as stream:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stream,
            cwd=str(REPO),
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("owned Bridge exited before IPC readiness")
            if resource.socket_path.is_socket():
                item = resource.socket_path.stat()
                if item.st_uid != os.getuid() or stat.S_IMODE(item.st_mode) != 0o600:
                    raise RuntimeError("owned Bridge socket permissions are not 0600")
                return proc
            time.sleep(0.1)
        raise RuntimeError("owned Bridge IPC readiness timed out")
    except BaseException:
        shutdown(proc)
        raise


def start_core(
    identity: InstanceIdentity,
    config: Path,
    root: Path,
    port: int,
    log: Path,
    *,
    via_launcher: bool = False,
) -> subprocess.Popen:
    env = dict(os.environ)
    if via_launcher:
        argv = [
            str(instance_app_path(identity) / "Contents/Resources/serverfs-service-launcher"),
            identity.instance_id,
            "http-origin",
        ]
    else:
        env.update(json.loads((root / "http-origin-environment.json").read_text()))
        argv = [
            sys.executable,
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
        ]
    with log.open("wb") as stream:
        return subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stream,
            cwd=str(REPO),
            start_new_session=True,
        )


def wait_core(proc: subprocess.Popen, port: int) -> None:
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.monotonic() + 15
    with httpx2.Client(trust_env=False, timeout=1) as http:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("owned Core exited before readiness")
            try:
                if http.get(url).status_code == 401:
                    return
            except (httpx2.RequestError, OSError):
                pass
            time.sleep(0.1)
    raise RuntimeError("owned Core loopback HTTP readiness timed out")


async def probe_core(port: int, key: str) -> None:
    url = f"http://127.0.0.1:{port}/mcp"
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {key}"},
        timeout=8,
        trust_env=False,
    ) as client:
        async with Client(
            streamable_http_client(url, http_client=client, terminate_on_close=False),
            mode="2026-07-28",
            raise_exceptions=True,
        ) as mcp:
            tools = {tool.name for tool in (await mcp.list_tools()).tools}
            if not {"list_workdirs", "get_agent_task"} <= tools:
                raise RuntimeError("owned Core Agent tools are missing")
            roots = await mcp.call_tool("list_workdirs", {})
            if roots.is_error or roots.structured_content is None:
                raise RuntimeError("owned Core filesystem tool failed")
            if {item["alias"] for item in roots.structured_content["workdirs"]} != {"synthetic"}:
                raise RuntimeError("owned Core workdir isolation failed")
            # Do not submit any Agent task. Missing-task lookup must reach
            # the named Bridge, not another instance or an unavailable socket.
            result = await mcp.call_tool(
                "get_agent_task", {"task_id": "agt_" + secrets.token_hex(12)}
            )
            content = str(result.structured_content or result.content)
            if not result.is_error or "AGENT_TASK_NOT_FOUND" not in content:
                raise RuntimeError("owned Core did not reach its own Bridge task store")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--via-launcher",
        action="store_true",
        help="execute each signed native ServiceLauncher without launchd registration",
    )
    args = parser.parse_args()
    if not args.execute:
        print("NOT_EXECUTED: explicit --execute required")
        return 2
    via_launcher = args.via_launcher
    baseline = tuple(_launchd_job_pid(label) for label in PROTECTED)
    if any(pid is None for pid in baseline):
        raise RuntimeError("production launchd baseline is incomplete")
    instance_id = "v25-oc-" + secrets.token_hex(5)
    identity = InstanceIdentity(instance_id=instance_id, bridge_mode="owned", bridge_id=instance_id)
    root = instance_service_home(identity)
    owner = root.parent
    if owner.exists() or owner.is_symlink():
        raise RuntimeError("synthetic instance owner is occupied")
    core = bridge = None
    reached = False
    removed = False
    socket_removed = False
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-owned-runtime-") as temp:
        base = Path(temp).resolve()
        try:
            workdir = base / "workdir"
            workdir.mkdir(mode=0o700)
            secrets_dir = base / "secrets"
            secrets_dir.mkdir(mode=0o700)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            key = "synthetic-" + secrets.token_hex(16)
            config = base / "serverfs.toml"
            config.write_text(
                "[agent]\nenabled=true\n[agent.codex]\nenabled=true\n"
                "[[workdirs]]\nalias='synthetic'\n"
                f"path='{workdir}'\nread_only=false\n"
                "agent_mode='workspace-write'\nagent_runtimes=['codex']\n"
            )
            env_file = secrets_dir / "http.env"
            env_file.write_text(
                f"SERVERFS_HTTP_API_KEY={key}\n"
                f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{port}\n"
            )
            env_file.chmod(0o600)
            output = base / "build"
            subprocess.run(
                ["bash", str(BUILD), str(output)],
                capture_output=True,
                check=True,
                timeout=120,
            )
            stage_owned_core_bridge(
                identity,
                config,
                env_file,
                port,
                "synthetic.test",
                output / APP_NAME,
                BRIDGE_PYTHON,
            )
            resource = named_owned_bridge_resources(identity)
            bridge = start_bridge(
                identity, resource, base / "bridge.stderr.log", via_launcher=via_launcher
            )
            core = start_core(
                identity,
                config,
                root,
                port,
                base / "core.stderr.log",
                via_launcher=via_launcher,
            )
            wait_core(core, port)
            with httpx2.Client(trust_env=False, timeout=2) as http:
                url = f"http://127.0.0.1:{port}/mcp"
                if (
                    http.get(url).status_code != 401
                    or http.get(url, headers={"Authorization": "Bearer incorrect"}).status_code
                    != 401
                ):
                    raise RuntimeError("owned Core authentication gate failed")
            asyncio.run(probe_core(port, key))
            reached = True
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc))
        finally:
            shutdown(core)
            shutdown(bridge)
            resource = named_owned_bridge_resources(identity)
            directory = resource.socket_path.parent
            if directory.is_dir() and not directory.is_symlink():
                try:
                    directory.rmdir()
                except OSError:
                    errors.append("owned Bridge socket directory not empty")
            socket_removed = not directory.exists()
            if owner.is_dir() and not owner.is_symlink():
                try:
                    if owner.parent.name != "instances" or owner.name != instance_id:
                        raise RuntimeError("synthetic owner cleanup identity mismatch")
                    shutil.rmtree(owner)
                except OSError:
                    errors.append("synthetic owner cleanup refused")
            removed = not owner.exists()
            if tuple(_launchd_job_pid(label) for label in PROTECTED) != baseline:
                errors.append("production PID baseline changed")
    payload = {
        "result": (
            "PASS" if reached and not errors and removed and socket_removed else "NOT_ACCEPTED"
        ),
        "core_to_owned_bridge_rpc": reached,
        "native_signed_launcher_used": via_launcher,
        "processes_stopped": all(p is None or p.poll() is not None for p in (core, bridge)),
        "private_owner_removed": removed,
        "socket_namespace_removed": socket_removed,
        "production_pids_unchanged": (
            tuple(_launchd_job_pid(label) for label in PROTECTED) == baseline
        ),
        "errors": errors,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
