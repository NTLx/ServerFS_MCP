"""Opt-in Docker lifecycle acceptance with two offline local connector stand-ins."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("SERVERFS_LINUX_DOCKER_TEST_IMAGE", "")
USE_SOURCE = os.environ.get("SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE") == "1"
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not IMAGE,
    reason="requires an explicit cached Linux Docker acceptance image",
)

_STANDIN = r"""import asyncio
import json
import os
import time
import urllib.request

kind = os.environ["CONNECTOR_KIND"]
key = os.environ["SERVERFS_HTTP_API_KEY"]
url = "http://serverfs-mcp:8000/mcp"

def post(payload, session=None):
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        body = response.read().decode()
        session = response.headers.get("Mcp-Session-Id", session)
    if not body.strip():
        return {}, session
    if body.lstrip().startswith("data:") or "\ndata:" in body:
        body = next(line[5:].strip() for line in body.splitlines() if line.startswith("data:"))
    return json.loads(body), session

def stdlib_check():
    initialized, session = post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "offline-stdlib-connector", "version": "1"}},
    })
    assert initialized.get("result")
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    listed, session = post(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session
    )
    assert any(item["name"] == "read_text_file" for item in listed["result"]["tools"])
    called, _ = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "read_text_file", "arguments": {
                          "workdir": "fixture", "path": "fixture.txt"}}}, session)
    assert "dual-connector-fixture" in json.dumps(called)

async def sdk_check():
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {key}"}, trust_env=False
    ) as http:
        transport = streamable_http_client(url, http_client=http, terminate_on_close=False)
        async with Client(transport, mode="2026-07-28") as client:
            await client.list_tools()
            result = await client.call_tool("read_text_file", {
                "workdir": "fixture", "path": "fixture.txt"})
            assert "dual-connector-fixture" in str(result.content)

while True:
    if kind == "stdlib":
        stdlib_check()
    else:
        asyncio.run(sdk_check())
    with open("/tmp/sfs-ok", "w", encoding="utf-8") as heartbeat:
        heartbeat.write(str(time.time()))
    print(f"MCP_OK connector={kind} timestamp_ns={time.time_ns()}", flush=True)
    time.sleep(0.5)
"""


def test_two_offline_connectors_are_lifecycle_independent(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    assert docker is not None
    daemon = [docker, "--host", "unix:///var/run/docker.sock"]
    project = "serverfs-v0250-dual-" + uuid.uuid4().hex[:12]
    port = _reserve_port()
    key = "synthetic-" + uuid.uuid4().hex
    workdir = tmp_path / "work"
    workdir.mkdir(mode=0o700)
    (workdir / "fixture.txt").write_text("dual-connector-fixture", encoding="utf-8")
    disabled = tmp_path / "disabled"
    disabled.mkdir(mode=0o700)
    (disabled / ".serverfs-disabled").touch()
    env_file = tmp_path / "empty.env"
    env_file.touch(mode=0o600)
    cloudflare_token = tmp_path / "cloudflare-token"
    cloudflare_token.write_text("synthetic-cloudflare-token", encoding="utf-8")
    openai_key = tmp_path / "openai-key"
    openai_key.write_text("synthetic-openai-key", encoding="utf-8")
    core_bearer = tmp_path / "core-bearer"
    core_bearer.write_text(f"Bearer {key}", encoding="utf-8")
    for path in (cloudflare_token, openai_key, core_bearer):
        path.chmod(0o600)
    standin = tmp_path / "connector_standin.py"
    standin.write_text(_STANDIN, encoding="utf-8")
    core_overlay = ""
    if USE_SOURCE:
        source = tmp_path / "candidate-src"
        shutil.copytree(ROOT / "src", source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        core_overlay = (
            "  serverfs-mcp:\n"
            "    environment:\n"
            "      PYTHONPATH: /run/serverfs-candidate-src\n"
            "    volumes:\n"
            "      - type: bind\n"
            f'        source: "{source}"\n'
            "        target: /run/serverfs-candidate-src\n"
            "        read_only: true\n"
            "        bind:\n"
            "          create_host_path: false\n"
        )
    overlay = tmp_path / "offline-standins.yml"
    overlay.write_text(
        "services:\n" + core_overlay + "  cloudflared:\n"
        "    image: ${SERVERFS_LINUX_DOCKER_TEST_IMAGE:?}\n"
        "    entrypoint: [python, /tmp/serverfs-connector-standin.py]\n"
        "    command: [sdk]\n"
        "    tmpfs: [/tmp]\n"
        "    environment:\n"
        "      CONNECTOR_KIND: sdk\n"
        "      SERVERFS_HTTP_API_KEY: ${SERVERFS_HTTP_API_KEY:?}\n"
        "    volumes:\n"
        "      - type: bind\n"
        f'        source: "{standin}"\n'
        "        target: /tmp/serverfs-connector-standin.py\n"
        "        read_only: true\n"
        "        bind:\n"
        "          create_host_path: false\n"
        "    healthcheck:\n"
        "      test: [CMD, python, -c, \"assert __import__('os').path.exists('/tmp/sfs-ok')\"]\n"
        "  openai-tunnel:\n"
        "    image: ${SERVERFS_LINUX_DOCKER_TEST_IMAGE:?}\n"
        "    entrypoint: [python, /tmp/serverfs-connector-standin.py]\n"
        "    command: [stdlib]\n"
        "    tmpfs: [/tmp]\n"
        "    environment:\n"
        "      CONNECTOR_KIND: stdlib\n"
        "      SERVERFS_HTTP_API_KEY: ${SERVERFS_HTTP_API_KEY:?}\n"
        "    volumes:\n"
        "      - type: bind\n"
        f'        source: "{standin}"\n'
        "        target: /tmp/serverfs-connector-standin.py\n"
        "        read_only: true\n"
        "        bind:\n"
        "          create_host_path: false\n"
        "    healthcheck:\n"
        "      test: [CMD, python, -c, \"assert __import__('os').path.exists('/tmp/sfs-ok')\"]\n"
        "networks:\n"
        "  tunnel_egress:\n"
        "    internal: true\n",
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path),
        "COMPOSE_PROFILES": "cloudflare,openai-http",
        "SERVERFS_IMAGE": IMAGE,
        "SERVERFS_LINUX_DOCKER_TEST_IMAGE": IMAGE,
        "SERVERFS_UID": str(os.getuid()),
        "SERVERFS_GID": str(os.getgid()),
        "SERVERFS_HTTP_HOST": "0.0.0.0",
        "SERVERFS_HTTP_PORT": "8000",
        "SERVERFS_HTTP_PUBLISH_HOST": "127.0.0.1",
        "SERVERFS_HTTP_PUBLISH_PORT": str(port),
        "SERVERFS_HTTP_ALLOWED_HOSTS": f"127.0.0.1:{port},serverfs-mcp:8000",
        "SERVERFS_HTTP_API_KEY": key,
        "WORKDIR_01_ALIAS": "fixture",
        "WORKDIR_01_PATH": str(workdir),
        "WORKDIR_01_READ_ONLY": "true",
        "CLOUDFLARE_TUNNEL_TOKEN_FILE": str(cloudflare_token),
        "CONTROL_PLANE_TUNNEL_ID": "tunnel_" + "a" * 32,
        "CONTROL_PLANE_API_KEY": "synthetic-unused-openai-key",
        "OPENAI_CONTROL_PLANE_KEY_FILE": str(openai_key),
        "SERVERFS_CORE_BEARER_FILE": str(core_bearer),
    }
    for slot in range(2, 17):
        env[f"WORKDIR_{slot:02d}_PATH"] = str(disabled)

    files = [
        "compose.yml",
        "compose.http.yml",
        "deployment/linux/compose.internal.yml",
        "deployment/linux/compose.bridge-disabled.yml",
        "compose.cloudflare.yml",
        "deployment/linux/compose.cloudflare-profile.yml",
        "deployment/linux/compose.openai-http.yml",
        str(overlay),
    ]
    base = [*daemon, "compose", "--env-file", str(env_file), "-p", project]
    for compose_file in files:
        path = Path(compose_file)
        base.extend(("-f", str(path if path.is_absolute() else ROOT / path)))

    def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [*base, *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if check:
            assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
        return result

    def assert_no_source_overlay(core: dict, container: str | None = None) -> None:
        if USE_SOURCE:
            return
        assert "PYTHONPATH" not in core.get("environment", {})
        assert all(
            volume.get("target") != "/run/serverfs-candidate-src"
            for volume in core.get("volumes", [])
        )
        if container:
            env_result = subprocess.run(
                [*daemon, "inspect", "--format", "{{json .Config.Env}}", container],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            mount_result = subprocess.run(
                [*daemon, "inspect", "--format", "{{json .Mounts}}", container],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            assert all(
                not value.startswith("PYTHONPATH=") for value in json.loads(env_result.stdout)
            )
            assert all(
                mount.get("Destination") != "/run/serverfs-candidate-src"
                and "candidate-src" not in mount.get("Source", "")
                for mount in json.loads(mount_result.stdout)
            )

    def identity(service: str) -> tuple[str, str]:
        container = compose("ps", "-q", service).stdout.strip()
        assert container
        result = subprocess.run(
            [*daemon, "inspect", "--format", "{{.Id}} {{.State.StartedAt}}", container],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return tuple(result.stdout.strip().split(" ", 1))  # type: ignore[return-value]

    def logs(service: str) -> str:
        return compose("logs", "--no-color", service).stdout

    def wait_fresh_call(service: str, kind: str, after_ns: int) -> None:
        # Containers use the same host wall clock. Require a completed MCP
        # request after the lifecycle command, not an old retained log entry.
        marker = f"MCP_OK connector={kind} timestamp_ns="
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            matches = [line for line in logs(service).splitlines() if marker in line]
            timestamps = [int(line.split(marker, 1)[1].strip()) for line in matches]
            if max(timestamps, default=0) > after_ns:
                return
            time.sleep(0.25)
        detail = compose("logs", "--no-color", "--tail", "30", service, check=False)
        pytest.fail(
            f"{service} did not perform an authenticated MCP tool call:\n"
            + (detail.stdout + detail.stderr)[-4000:]
        )

    try:
        assert not compose("ps", "-a", "-q").stdout.strip()
        config = json.loads(compose("config", "--format", "json").stdout)
        services = config["services"]
        assert set(services) == {"serverfs-mcp", "cloudflared", "openai-tunnel"}
        core_config = services["serverfs-mcp"]
        assert_no_source_overlay(core_config)
        assert not core_config.get("ports")
        assert all(
            config["networks"][network].get("internal") for network in core_config["networks"]
        )
        assert set(services["cloudflared"]["networks"]) == {"serverfs_internal", "tunnel_egress"}
        assert set(services["openai-tunnel"]["networks"]) == {"serverfs_internal", "tunnel_egress"}
        assert config["networks"]["tunnel_egress"]["internal"] is True

        compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "serverfs-mcp")
        core_container = compose("ps", "-q", "serverfs-mcp").stdout.strip()
        assert core_container
        assert_no_source_overlay(core_config, core_container)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            health = subprocess.run(
                [*daemon, "inspect", "--format", "{{.State.Health.Status}}", core_container],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            if health.stdout.strip() == "healthy":
                break
            time.sleep(0.25)
        else:
            pytest.fail("internal-only Core did not become healthy")
        bindings = subprocess.run(
            [*daemon, "inspect", "--format", "{{json .HostConfig.PortBindings}}", core_container],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert json.loads(bindings.stdout) in (None, {})
        for connector in ("cloudflared", "openai-tunnel"):
            compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", connector)
        network_ids = subprocess.run(
            [
                *daemon,
                "network",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.splitlines()
        actual_networks = {}
        for network_id in network_ids:
            actual_network = subprocess.run(
                [
                    *daemon,
                    "network",
                    "inspect",
                    "--format",
                    '{{.Internal}} {{index .Labels "com.docker.compose.network"}}',
                    network_id,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            is_internal, logical_name = actual_network.stdout.strip().split(" ", 1)
            actual_networks[logical_name] = is_internal == "true"
        assert actual_networks.get("serverfs_internal") is True
        assert actual_networks.get("tunnel_egress") is True
        ready_after = time.time_ns()
        wait_fresh_call("cloudflared", "sdk", ready_after)
        wait_fresh_call("openai-tunnel", "stdlib", ready_after)

        for target, target_kind, sibling, sibling_kind in (
            ("cloudflared", "sdk", "openai-tunnel", "stdlib"),
            ("openai-tunnel", "stdlib", "cloudflared", "sdk"),
        ):
            core_identity = identity("serverfs-mcp")
            sibling_identity = identity(sibling)
            target_identity = identity(target)
            compose("stop", target)
            stopped_after = time.time_ns()
            assert not compose("ps", "-q", target).stdout.strip()
            assert identity("serverfs-mcp") == core_identity
            assert identity(sibling) == sibling_identity
            wait_fresh_call(sibling, sibling_kind, stopped_after)

            compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", target)
            started_after = time.time_ns()
            restarted_identity = identity(target)
            assert restarted_identity[0] == target_identity[0]
            assert restarted_identity[1] != target_identity[1]
            assert identity("serverfs-mcp") == core_identity
            assert identity(sibling) == sibling_identity
            wait_fresh_call(target, target_kind, started_after)
            wait_fresh_call(sibling, sibling_kind, started_after)

            compose(
                "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", target
            )
            recreated_after = time.time_ns()
            recreated_identity = identity(target)
            assert recreated_identity[0] != restarted_identity[0]
            assert identity("serverfs-mcp") == core_identity
            assert identity(sibling) == sibling_identity
            wait_fresh_call(target, target_kind, recreated_after)
            wait_fresh_call(sibling, sibling_kind, recreated_after)
    finally:
        compose("down", "--timeout", "5", "--remove-orphans", check=False)
        containers = subprocess.run(
            [*daemon, "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        networks = subprocess.run(
            [
                *daemon,
                "network",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert not containers.stdout.strip(), f"test project containers remain: {containers.stdout}"
        assert not networks.stdout.strip(), f"test project networks remain: {networks.stdout}"


def _reserve_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]
