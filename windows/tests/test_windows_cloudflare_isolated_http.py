"""Isolated Windows-native HTTP process acceptance without Cloudflare or Task Scheduler.

This exercises real socket binding, MCP admission and process containment on a
random loopback port with disposable read-only workdirs. It never touches the
persistent ServerFS Native Tunnel task, production secrets or public DNS.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows local HTTP process")

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "windows-isolated-http-test", "version": "1"},
    },
}


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str, *, key: str | None = None) -> tuple[int, bytes]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        url,
        method="POST",
        headers=headers,
        data=json.dumps(INITIALIZE).encode("utf-8"),
    )
    # A user-configured HTTP(S)_PROXY must never intercept this local probe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=2) as response:
            return response.status, response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(16 * 1024)


def test_native_http_isolated_real_process(tmp_path: Path) -> None:
    """Public/local auth behavior in a real subprocess, not a mocked ASGI app."""
    from serverfs_mcp.windows_job import WindowsJob

    importlib.import_module("serverfs_windows_native")  # fail if native wheel was not staged

    workdir = tmp_path / "read-only-fixture"
    workdir.mkdir()
    (workdir / "hello.txt").write_text("fixture\n", encoding="utf-8")
    config = tmp_path / "serverfs.toml"
    config.write_text(
        "[[workdirs]]\nalias = 'fixture'\npath = "
        + json.dumps(str(workdir))
        + "\nread_only = true\n",
        encoding="utf-8",
    )
    port = _pick_port()
    url = f"http://127.0.0.1:{port}/mcp"
    secret = "local-isolated-test-key"
    env = os.environ.copy()
    env.update(
        {
            "SERVERFS_HTTP_API_KEY": secret,
            "SERVERFS_HTTP_ALLOWED_HOSTS": f"127.0.0.1:{port}",
            "SERVERFS_OAUTH_ENABLED": "false",
        }
    )
    log_path = tmp_path / "http-process.log"
    with log_path.open("wb") as log, WindowsJob() as job:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "serverfs_mcp.cli",
                "serve",
                "--config",
                str(config),
                "--transport",
                "streamable-http",
                "--http-port",
                str(port),
            ],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_SUSPENDED", 0x4)
            ),
        )
        try:
            job.assign(process)
            job.resume(process)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("isolated HTTP process exited unexpectedly")
                try:
                    status, _ = _request(url)
                except (OSError, TimeoutError):
                    time.sleep(0.15)
                    continue
                if status == 401:
                    break
                pytest.fail(f"isolated anonymous admission returned HTTP {status}, not 401")
            else:
                pytest.fail("isolated HTTP did not become ready in 15 seconds")
            status, body = _request(url, key=secret)
            assert status == 200
            assert b"serverInfo" in body
            assert secret.encode() not in body
        finally:
            job.close()
            process.wait(timeout=10)
    assert secret not in log_path.read_text(encoding="utf-8", errors="replace")
