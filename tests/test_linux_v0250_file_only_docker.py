"""Opt-in Linux acceptance for a Bridge-disabled Docker Core."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from test_linux_v0250_http_e2e import _http_status, _port, _tool

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("SERVERFS_LINUX_DOCKER_TEST_IMAGE", "")
USE_SOURCE = os.environ.get("SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE") == "1"
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not IMAGE,
    reason="requires explicit local Docker acceptance image",
)


def test_bridge_disabled_direct_and_internal_http(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    assert docker is not None
    daemon = [docker, "--host", "unix:///var/run/docker.sock"]
    project = "serverfs-v0250-file-" + uuid.uuid4().hex[:12]
    port = _port()
    api_key = "synthetic-" + uuid.uuid4().hex
    workdir = tmp_path / "work"
    workdir.mkdir(mode=0o700)
    (workdir / "fixture.txt").write_text("file-only-fixture", encoding="utf-8")
    empty_env = tmp_path / "empty.env"
    empty_env.touch(mode=0o600)
    source_overlay = None
    if USE_SOURCE:
        source = tmp_path / "candidate-src"
        shutil.copytree(ROOT / "src", source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        source_overlay = tmp_path / "source.yml"
        source_overlay.write_text(
            "services:\n  serverfs-mcp:\n    environment:\n"
            "      PYTHONPATH: /run/serverfs-candidate-src\n    volumes:\n"
            '      - type: bind\n        source: "' + str(source) + '"\n'
            "        target: /run/serverfs-candidate-src\n        read_only: true\n"
            "        bind:\n          create_host_path: false\n",
            encoding="utf-8",
        )
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path),
        "SERVERFS_IMAGE": IMAGE,
        "SERVERFS_UID": str(os.getuid()),
        "SERVERFS_GID": str(os.getgid()),
        "SERVERFS_HTTP_HOST": "0.0.0.0",
        "SERVERFS_HTTP_PORT": "8000",
        "SERVERFS_HTTP_PUBLISH_HOST": "127.0.0.1",
        "SERVERFS_HTTP_PUBLISH_PORT": str(port),
        "SERVERFS_HTTP_ALLOWED_HOSTS": f"127.0.0.1:{port},127.0.0.1:8000",
        "SERVERFS_HTTP_API_KEY": api_key,
        "WORKDIR_01_ALIAS": "fixture",
        "WORKDIR_01_PATH": str(workdir),
        "WORKDIR_01_READ_ONLY": "false",
    }
    for slot in range(2, 17):
        disabled = tmp_path / f"disabled-{slot}"
        disabled.mkdir()
        (disabled / ".serverfs-disabled").touch()
        env[f"WORKDIR_{slot:02d}_PATH"] = str(disabled)
    base = [*daemon, "compose", "--env-file", str(empty_env), "-p", project]
    files = [
        "compose.yml",
        "compose.http.yml",
        "deployment/linux/compose.publish-http.yml",
        "deployment/linux/compose.bridge-disabled.yml",
    ]
    if source_overlay is not None:
        files.append(str(source_overlay))
    for path in files:
        base.extend(("-f", str(ROOT / path if not Path(path).is_absolute() else path)))
    internal_base = [*daemon, "compose", "--env-file", str(empty_env), "-p", project]
    for path in [
        "compose.yml",
        "compose.http.yml",
        "deployment/linux/compose.internal.yml",
        "deployment/linux/compose.bridge-disabled.yml",
    ]:
        internal_base.extend(("-f", str(ROOT / path if not Path(path).is_absolute() else path)))
    if source_overlay is not None:
        internal_base.extend(("-f", str(source_overlay)))

    def assert_no_source_overlay(core: dict, container: str | None = None) -> None:
        if USE_SOURCE:
            return
        assert "PYTHONPATH" not in core.get("environment", {})
        assert all(
            volume.get("target") != "/run/serverfs-candidate-src" for volume in core["volumes"]
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

    def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [*base, *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if check:
            assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
        return result

    def compose_internal(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [*internal_base, *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if check:
            assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
        return result

    def wait_for(status: int, token: str = api_key) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if _http_status(port, token) == status:
                return
            time.sleep(0.2)
        pytest.fail(f"HTTP endpoint did not reach status {status}")

    def call_tool(name: str, arguments: dict | None = None):
        return asyncio.run(_tool(port, api_key, name, arguments))

    def internal_mcp(container: str) -> str:
        script = (
            "import asyncio, httpx2\n"
            "from mcp import Client\n"
            "from mcp.client.streamable_http import streamable_http_client\n"
            "async def main():\n"
            f"  async with httpx2.AsyncClient(\n"
            f"      headers={{'Authorization': 'Bearer {api_key}'}}, trust_env=False) as http:\n"
            "    transport = streamable_http_client(\n"
            "        'http://127.0.0.1:8000/mcp', http_client=http, terminate_on_close=False)\n"
            "    async with Client(transport, mode='2026-07-28') as client:\n"
            "      tools = await client.list_tools()\n"
            "      assert any(tool.name == 'read_text_file' for tool in tools.tools)\n"
            "      result = await client.call_tool(\n"
            "          'read_text_file', {'workdir': 'fixture', 'path': 'fixture.txt'})\n"
            "      assert 'file-only-fixture' in str(result.content)\n"
            "asyncio.run(main())\n"
        )
        result = subprocess.run(
            [*daemon, "exec", container, "python", "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        return result.stdout

    try:
        assert not compose("ps", "-a", "-q").stdout.strip()
        config = json.loads(compose("config", "--format", "json").stdout)
        core = config["services"]["serverfs-mcp"]
        assert core.get("ports", [])[0]["host_ip"] == "127.0.0.1"
        assert core["environment"]["SERVERFS_AGENT_BRIDGE_ENABLED"] == "false"
        assert_no_source_overlay(core)
        assert all(
            volume.get("target") not in {"/run/serverfs-agent-bridge", "/run/serverfs-agent-locks"}
            for volume in core["volumes"]
        )
        compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "serverfs-mcp")
        published_container = compose("ps", "-q", "serverfs-mcp").stdout.strip()
        assert published_container
        assert_no_source_overlay(core, published_container)
        wait_for(401, "wrong-synthetic-key")
        assert {"read_text_file", "create_text_file"} <= call_tool("tools/list")
        read = call_tool("read_text_file", {"workdir": "fixture", "path": "fixture.txt"})
        assert not read.is_error and "file-only-fixture" in str(read.content)
        written = call_tool(
            "create_text_file",
            {"workdir": "fixture", "path": "written.txt", "content": "synthetic-write"},
        )
        assert not written.is_error and (workdir / "written.txt").read_text() == "synthetic-write"

        compose("stop", "serverfs-mcp")
        internal = compose_internal("config", "--format", "json")
        internal_config = json.loads(internal.stdout)
        internal_core = internal_config["services"]["serverfs-mcp"]
        assert_no_source_overlay(internal_core)
        assert not internal_core.get("ports")
        assert all(
            internal_config["networks"][name].get("internal") for name in internal_core["networks"]
        )
        compose_internal("up", "-d", "--no-deps", "--no-build", "--pull", "never", "serverfs-mcp")
        container = compose("ps", "-q", "serverfs-mcp").stdout.strip()
        assert container
        assert_no_source_overlay(internal_core, container)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            health = subprocess.run(
                [*daemon, "inspect", "--format", "{{.State.Health.Status}}", container],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            if health.stdout.strip() == "healthy":
                break
            time.sleep(0.2)
        else:
            pytest.fail("internal-only Core did not become healthy")
        assert internal_mcp(container) == ""
        bindings = subprocess.run(
            [*daemon, "inspect", "--format", "{{json .HostConfig.PortBindings}}", container],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert json.loads(bindings.stdout) in (None, {})
    finally:
        compose("down", "--timeout", "3", "--remove-orphans", check=False)
        remaining = subprocess.run(
            [*daemon, "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert not remaining.stdout.strip(), f"owned project containers remain: {remaining.stdout}"
        remaining_networks = subprocess.run(
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
        assert not remaining_networks.stdout.strip(), (
            f"owned project networks remain: {remaining_networks.stdout}"
        )
