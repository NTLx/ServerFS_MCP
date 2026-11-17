"""Disposable real macOS file-only Core HTTP E2E, no launchd registration.

Builds and signs a named app, stages only synthetic/private Core state, then
launches only the staged Core argv as this script's child process. Uses a local
random 127.0.0.1 TCP port and an SDK MCP client. No production credentials,
workdirs, Connectors, Agent Bridge, or existing launchd service are touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from serverfs_mcp.darwin_instance_runtime import stage_file_only_instance
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity


async def _mcp_roundtrip(url: str, key: str) -> dict:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {key}"}, trust_env=False, timeout=10.0
    ) as http_client:
        transport = streamable_http_client(url, http_client=http_client)
        async with Client(transport, mode="2026-07-28", raise_exceptions=True) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "list_workdirs" in names
            assert "read_text_file" in names
            assert "create_text_file" in names
            assert "delete_file" in names
            assert "submit_agent_task" not in names
            assert "list_agent_runtimes" not in names

            listed = await client.call_tool("list_workdirs", {})
            workdirs = listed.structured_content["workdirs"]
            assert len(workdirs) == 1
            assert workdirs[0]["alias"] == "isolated"
            assert workdirs[0]["access"] == "read-write"
            existing = await client.call_tool(
                "read_text_file", {"workdir": "isolated", "path": "hello.txt"}
            )
            assert "synthetic-file-only" in json.dumps(existing.structured_content)
            created = await client.call_tool(
                "create_text_file",
                {"workdir": "isolated", "path": "probe.txt", "content": "probe-phase1"},
            )
            revision = created.structured_content["revision"]
            assert isinstance(revision, str) and revision
            changed = await client.call_tool(
                "read_text_file", {"workdir": "isolated", "path": "probe.txt"}
            )
            assert "probe-phase1" in json.dumps(changed.structured_content)
            removed = await client.call_tool(
                "delete_file",
                {
                    "workdir": "isolated",
                    "path": "probe.txt",
                    "expected_revision": revision,
                },
            )
            assert not removed.is_error
            return {"tools": len(names), "file_only": True, "revision_crud": True}


def _smoke() -> None:
    repo = Path(__file__).resolve().parents[2]
    identity = InstanceIdentity(instance_id="v25-http-smoke", bridge_mode="disabled")
    key = "synthetic-smoke-key-4459"
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-http-smoke-") as temporary:
        root = Path(temporary).resolve(strict=True)
        home = root / "user"
        base = home / "Library/Application Support/ServerFS"
        base.mkdir(parents=True, mode=0o700)
        base.chmod(0o700)
        workdir = root / "workdir"
        workdir.mkdir(mode=0o700)
        (workdir / "hello.txt").write_text("synthetic-file-only", encoding="utf-8")
        config = root / "core.toml"
        config.write_text(
            f"[[workdirs]]\nalias = 'isolated'\npath = '{workdir}'\nread_only = false\n",
            encoding="utf-8",
        )
        private = root / "operator"
        private.mkdir(mode=0o700)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        envfile = private / "core.env"
        envfile.write_text(
            f"SERVERFS_HTTP_API_KEY={key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS=smoke.invalid,127.0.0.1:{port}\n",
            encoding="utf-8",
        )
        envfile.chmod(0o600)
        build = root / "build"
        result = subprocess.run(
            ["bash", str(repo / "native/macos/service_manager/build.sh"), str(build)],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0
        stage_file_only_instance(
            identity, config, envfile, port, "smoke.invalid", build / APP_NAME, home=home
        )
        state = instance_service_home(identity, home=home)
        manifest = json.loads((state / "services.json").read_text())
        service = manifest["services"]["http-origin"]
        snapshot = json.loads((state / "http-origin-environment.json").read_text())
        # Deliberately construct a minimal, credential-free inherited environment.
        environment = {
            k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ
        }
        environment.update(snapshot)
        process = subprocess.Popen(
            [service["executable"], *service["arguments"]],
            cwd=service["working_directory"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            url = f"http://127.0.0.1:{port}/mcp"
            deadline = time.monotonic() + 15
            ready = False
            with httpx2.Client(trust_env=False, timeout=1.5) as client:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError("ephemeral Core exited before readiness")
                    try:
                        response = client.get(url)
                        if response.status_code == 401:
                            ready = True
                            break
                    except httpx2.RequestError:
                        pass
                    time.sleep(0.1)
                assert ready, "ephemeral HTTP Core did not return anonymous 401"
                assert (
                    client.get(url, headers={"Authorization": "Bearer bad-key"}).status_code == 401
                )
            outcome = asyncio.run(_mcp_roundtrip(url, key))
            assert not (workdir / "probe.txt").exists()
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "mode": "disabled",
                        "anonymous_401": True,
                        "bad_key_401": True,
                        "registered": False,
                        "process_owned_by_test": True,
                        **outcome,
                    },
                    sort_keys=True,
                )
            )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    _smoke()
