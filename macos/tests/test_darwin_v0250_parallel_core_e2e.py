"""Real macOS v0.25 dual file-only Core smoke; no launchd or Tunnel changes.

Two native `serve` children bind distinct loopback ports and serve distinct
private configurations. The test cleans both child processes on every exit.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native macOS only")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _unauthenticated_http_code(port: int, *, key: str | None = None) -> int:
    headers = {} if key is None else {"Authorization": f"Bearer {key}"}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=0.5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, TimeoutError):
        return 0


@contextmanager
def _core_process(base: Path, instance: str, port: int, token: str):
    folder = base / instance
    folder.mkdir(mode=0o700)
    workdir = folder / "workdir"
    workdir.mkdir()
    data_home = folder / "data"
    data_home.mkdir(mode=0o700)
    conf = folder / "serverfs.toml"
    conf.write_text(f'[[workdirs]]\nalias = "{instance}"\npath = "{workdir}"\nread_only = false\n')
    logs = folder / "stderr.txt"
    # The child has only this synthetic API credential and isolated data root;
    # do not inherit unrelated ServerFS/agent/Tunnel runtime configuration.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SERVERFS_", "CONTROL_PLANE_"))
    }
    env.update(
        {
            "SERVERFS_HTTP_API_KEY": token,
            "SERVERFS_HTTP_ALLOWED_HOSTS": f"{instance}.example.test,127.0.0.1:{port}",
            "SERVERFS_DATA_HOME": str(data_home),
            "SERVERFS_FILE_INGRESS_ENABLED": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    with logs.open("wb") as err:
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "serverfs_mcp.cli",
                "serve",
                "--config",
                str(conf),
                "--transport",
                "streamable-http",
                "--http-port",
                str(port),
            ],
            cwd=base,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise AssertionError(
                        f"isolated {instance} Core exited prematurely: code={child.returncode}"
                    )
                if _unauthenticated_http_code(port) == 401:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(f"isolated {instance} Core did not bind its port")
            yield f"http://127.0.0.1:{port}/mcp", workdir, data_home
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


async def _exercise_endpoint(url: str, token: str, alias: str) -> dict[str, object]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=8,
    ) as http_client:
        transport = streamable_http_client(url, http_client=http_client, terminate_on_close=False)
        async with Client(transport, mode="2026-07-28", raise_exceptions=True) as client:
            tools = await client.list_tools()
            assert "create_text_file" in {tool.name for tool in tools.tools}
            roots = await client.call_tool("list_workdirs", {})
            result = roots.structured_content
            assert result is not None
            assert {item["alias"] for item in result["workdirs"]} == {alias}
            result = await client.call_tool(
                "create_text_file",
                {"workdir": alias, "path": f"{alias}.txt", "content": f"owned-by-{alias}"},
            )
            assert not result.is_error
            read = await client.call_tool(
                "read_text_file", {"workdir": alias, "path": f"{alias}.txt"}
            )
            assert not read.is_error
            assert read.structured_content is not None
            assert read.structured_content["content"] == f"owned-by-{alias}"
            return {"alias": alias, "path": f"{alias}.txt"}


def test_two_real_file_only_core_processes_are_isolated(tmp_path: Path) -> None:
    """No native service registration, public network or installed credentials."""
    a_port, b_port = _free_port(), _free_port()
    while b_port == a_port:
        b_port = _free_port()
    a_token = "v0250-local-test-key-for-a"
    b_token = "v0250-local-test-key-for-b"

    with ExitStack() as stack:
        a_url, a_root, a_data = stack.enter_context(
            _core_process(tmp_path, "alpha", a_port, a_token)
        )
        b_url, b_root, b_data = stack.enter_context(
            _core_process(tmp_path, "bravo", b_port, b_token)
        )
        assert _unauthenticated_http_code(a_port, key=b_token) == 401
        assert _unauthenticated_http_code(b_port, key=a_token) == 401

        async def parallel_roundtrip():
            return await asyncio.gather(
                _exercise_endpoint(a_url, a_token, "alpha"),
                _exercise_endpoint(b_url, b_token, "bravo"),
            )

        outcomes = asyncio.run(parallel_roundtrip())
        assert {item["alias"] for item in outcomes} == {"alpha", "bravo"}
        assert (a_root / "alpha.txt").read_text() == "owned-by-alpha"
        assert (b_root / "bravo.txt").read_text() == "owned-by-bravo"
        assert not (a_root / "bravo.txt").exists()
        assert not (b_root / "alpha.txt").exists()
        assert a_data.is_dir() and b_data.is_dir() and a_data != b_data
