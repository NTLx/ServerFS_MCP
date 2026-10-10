#!/usr/bin/env python3
"""Linux v0.12 live OpenAI Tunnel direct/proxy acceptance.

The probe runs the official tunnel-client `run` path through the repository launcher, but replaces
CONTROL_PLANE_TUNNEL_ID with a fresh nonexistent value so it cannot attach to the production tunnel.
Each disposable container is bounded and forcibly removed. No secret, tunnel id or proxy URL is
printed.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from v012_connect_forwarder import ConnectForwarder  # noqa: E402

DEFAULT_IMAGE = "ghcr.io/openai/tunnel-client:v0.0.14"
_LAUNCHER = REPO_ROOT / "deployment" / "tunnel" / "tunnel-launcher.sh"


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        values[key.strip()] = _unquote(value)
    return values


def clean_parent_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        and key != "CONTROL_PLANE_HTTP_PROXY"
    }


def nonexistent_tunnel_id(real_id: str) -> str:
    try:
        uuid.UUID(real_id)
    except ValueError:
        if "_" in real_id:
            prefix, body = real_id.rsplit("_", 1)
            prefix += "_"
        else:
            prefix, body = "", real_id
        alphabet = string.ascii_lowercase + string.digits
        length = max(len(body), 16)
        return prefix + "".join(secrets.choice(alphabet) for _ in range(length))
    return str(uuid.uuid4())


def run_probe(
    *,
    mode: str,
    image: str,
    api_key: str,
    base_url: str,
    fake_tunnel_id: str,
) -> dict[str, int | bool]:
    observer = ConnectForwarder()
    observer.start()
    if observer.port is None:
        observer.stop()
        raise RuntimeError("proxy observer did not expose a port")

    env = clean_parent_env()
    env.update(
        {
            "CONTROL_PLANE_API_KEY": api_key,
            "CONTROL_PLANE_TUNNEL_ID": fake_tunnel_id,
            "CONTROL_PLANE_BASE_URL": base_url,
            "MCP_SERVER_URL": "http://127.0.0.1:9/mcp",
            "MCP_STARTUP_WAIT_TIMEOUT": "2s",
            "HEALTH_LISTEN_ADDR": "127.0.0.1:0",
            "SERVERFS_OPENAI_TUNNEL_USE_PROXY": "true" if mode == "proxy" else "false",
        }
    )
    pass_env = [
        "CONTROL_PLANE_API_KEY",
        "CONTROL_PLANE_TUNNEL_ID",
        "CONTROL_PLANE_BASE_URL",
        "MCP_SERVER_URL",
        "MCP_STARTUP_WAIT_TIMEOUT",
        "HEALTH_LISTEN_ADDR",
        "SERVERFS_OPENAI_TUNNEL_USE_PROXY",
    ]
    if mode == "proxy":
        env["SERVERFS_PROXY_HOST"] = "127.0.0.1"
        env["SERVERFS_PROXY_PORT"] = str(observer.port)
        pass_env.extend(["SERVERFS_PROXY_HOST", "SERVERFS_PROXY_PORT"])

    container_name = f"serverfs-v012-tunnel-{mode}-{os.getpid()}"
    argv = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        container_name,
        "--network",
        "host",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--entrypoint",
        "/bin/sh",
        "-v",
        f"{_LAUNCHER}:/opt/serverfs/tunnel-launcher.sh:ro",
    ]
    for name in pass_env:
        argv.extend(["-e", name])
    argv.extend([image, "/opt/serverfs/tunnel-launcher.sh"])

    process = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 6.0
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if mode == "proxy" and observer.summary().external_connects >= 1:
                break
            time.sleep(0.1)
        summary = observer.summary()
        exited = process.poll() is not None
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        observer.stop()

    return {
        "exited": exited,
        "total_connects": summary.total_connects,
        "external_connects": summary.external_connects,
        "loopback_connects": summary.loopback_connects,
    }


def main() -> int:
    if sys.platform != "linux":
        raise SystemExit("this acceptance is Linux-only")
    values = load_env(REPO_ROOT / ".env")
    api_key = values.get("CONTROL_PLANE_API_KEY", "")
    real_tunnel_id = values.get("CONTROL_PLANE_TUNNEL_ID", "")
    base_url = values.get("CONTROL_PLANE_BASE_URL", "") or "https://api.openai.com"
    image = values.get("OPENAI_TUNNEL_IMAGE", "") or DEFAULT_IMAGE
    if not api_key or not real_tunnel_id:
        raise RuntimeError("control-plane key/tunnel id are not configured")
    fake_id = nonexistent_tunnel_id(real_tunnel_id)
    if fake_id == real_tunnel_id:
        raise RuntimeError("failed to derive a non-production tunnel id")

    direct = run_probe(
        mode="direct",
        image=image,
        api_key=api_key,
        base_url=base_url,
        fake_tunnel_id=fake_id,
    )
    if direct["total_connects"] != 0:
        raise RuntimeError("Tunnel direct mode unexpectedly used the proxy observer")
    emit("tunnel_probe_pass", mode="direct", **direct)

    proxied = run_probe(
        mode="proxy",
        image=image,
        api_key=api_key,
        base_url=base_url,
        fake_tunnel_id=fake_id,
    )
    if proxied["external_connects"] < 1:
        raise RuntimeError("Tunnel proxy mode produced no external CONNECT traffic")
    if proxied["loopback_connects"] != 0:
        raise RuntimeError("Tunnel proxy mode attempted to proxy a loopback destination")
    emit("tunnel_probe_pass", mode="proxy", **proxied)
    emit("verdict", answer="PASS", case="tunnel_proxy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
