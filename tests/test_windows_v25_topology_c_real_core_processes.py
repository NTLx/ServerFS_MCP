"""Isolated topology C: two real Windows Core HTTP processes and Bridge workers.

Uses the product HTTP MCP SDK and true loopback TCP listeners, but bypasses
Task Scheduler and the installed v25 manifest owner. No real Agent provider,
Cloudflare token, remote ingress or native file CRUD. All credentials and
processes are disposable; both HTTP workdirs remain read-only.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path

import pytest

from serverfs_mcp.agent_lifecycle import (
    await_graceful_exit,
    bootstrap_frame_bytes,
    bridge_child_environment,
    request_graceful_shutdown,
    spawn_bridge,
)
from serverfs_mcp.windows_job import WindowsJob
from serverfs_mcp.windows_v25.bridge_v2_owner import wait_for_signed_bridge_v2_ready
from test_windows_v25_bridge_v2_real_worker import _test_installation
from test_windows_v25_two_independent_bridge_workers import _client, _ping

if sys.platform != "win32":
    pytest.skip("native Windows Core/Bridge processes required", allow_module_level=True)

_WORKER = Path(__file__).with_name("v25_temp_core_worker.py")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_loopback_ports() -> tuple[int, int]:
    # Reserve both at once: the OS must never select the same ephemeral port.
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as first,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second,
    ):
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return int(first.getsockname()[1]), int(second.getsockname()[1])


def _post(
    port: int,
    token: str | None,
    request: dict,
    *,
    session: str = "",
    host: str | None = None,
    origin: str | None = None,
) -> tuple[int, dict[str, str], dict | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Host": host or f"127.0.0.1:{port}",
    }
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(request).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=4) as response:
            code = response.status
            response_headers = dict(response.headers.items())
            body = response.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        code = exc.code
        response_headers = dict(exc.headers.items())
        body = exc.read(256 * 1024)
    payload = None
    for line in body.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[6:])
    if payload is None and body.startswith(b"{"):
        payload = json.loads(body)
    return code, response_headers, payload


def _initialize(port: int, token: str) -> str:
    code, headers, payload = _post(
        port,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "topology-c-process-gate", "version": "1"},
            },
        },
    )
    assert code == 200
    assert payload["result"]["serverInfo"]["name"] == "ServerFS"
    session = next(value for name, value in headers.items() if name.lower() == "mcp-session-id")
    code, _, _ = _post(
        port,
        token,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session=session,
    )
    assert code == 202
    return session


def _listed_tools(port: int, token: str, session: str) -> set[str]:
    code, _, payload = _post(
        port,
        token,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        session=session,
    )
    assert code == 200
    return {item["name"] for item in payload["result"]["tools"]}


def _workdirs(port: int, token: str, session: str) -> dict:
    code, _, payload = _post(
        port,
        token,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_workdirs", "arguments": {}},
        },
        session=session,
    )
    assert code == 200
    assert payload is not None
    assert payload["result"].get("isError") is not True
    return payload


def _launch_core(root: Path, *, port: int, token: str) -> subprocess.Popen:
    # No ambient Claude/Codex/Tunnel/proxy/credential variables reach Core.
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "USERPROFILE",
        "APPDATA",
        "PROGRAMDATA",
        "SYSTEMDRIVE",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["SERVERFS_TEMP_CORE_TOKEN"] = token
    return subprocess.Popen(
        [sys.executable, "-I", "-B", str(_WORKER), str(root), str(port)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _wait_core(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        assert process.poll() is None, "isolated Core worker stopped before readiness"
        try:
            code, _, _ = _post(
                port,
                None,
                {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            )
            if code == 401:
                return
        except (OSError, urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.12)
    raise AssertionError("isolated authenticated Core loopback listener unavailable")


def test_two_real_core_and_bridge_process_pairs_are_independent(tmp_path: Path) -> None:
    installed = []
    for name in ("first", "second"):
        private = tmp_path / name
        private.mkdir()
        plan, keys = _test_installation(private)
        installed.append((plan, keys, private / "read-only"))
    (first, keys_a, root_a), (second, keys_b, root_b) = installed
    port_a, port_b = _free_loopback_ports()
    assert port_a != port_b
    token_a, token_b = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    assert token_a != token_b

    bridges: list[subprocess.Popen] = []
    cores: list[subprocess.Popen] = []
    with ExitStack() as stack:
        job_a = stack.enter_context(WindowsJob(first.job_name))
        job_b = stack.enter_context(WindowsJob(second.job_name))
        try:
            for plan, job in ((first, job_a), (second, job_b)):
                bridge = spawn_bridge(
                    config_path=plan.config_path,
                    child_env=bridge_child_environment(None),
                    job=job,
                    argv=[
                        str(plan.bridge_python),
                        "-I",
                        "-B",
                        "-m",
                        "serverfs_agent_bridge.main",
                        "--config",
                        str(plan.config_path),
                        "--supervised",
                    ],
                )
                bridges.append(bridge)
                assert bridge.stdin is not None
                frame = bootstrap_frame_bytes(None, prior_bridge_execution_stopped=True)
                assert bridge.stdin.write(frame) == len(frame)
                bridge.stdin.flush()
                asyncio.run(
                    wait_for_signed_bridge_v2_ready(
                        plan, bridge, server_pid_validator=job.contains_pid, timeout=15.0
                    )
                )

            cores.append(_launch_core(root_a, port=port_a, token=token_a))
            cores.append(_launch_core(root_b, port=port_b, token=token_b))
            core_a, core_b = cores
            _wait_core(core_a, port_a)
            _wait_core(core_b, port_b)
            assert core_a.pid != core_b.pid
            assert all(not job.contains_pid(core.pid) for job in (job_a, job_b) for core in cores)

            session_a = _initialize(port_a, token_a)
            session_b = _initialize(port_b, token_b)
            assert session_a != session_b
            assert "first" in json.dumps(_workdirs(port_a, token_a, session_a))
            assert "second" in json.dumps(_workdirs(port_b, token_b, session_b))
            assert "second" not in json.dumps(_workdirs(port_a, token_a, session_a))
            assert "first" not in json.dumps(_workdirs(port_b, token_b, session_b))

            # File-only Cores must never gain Agent/Repo Ops through the
            # mere presence of independently running Bridge processes.
            for port, token, session in (
                (port_a, token_a, session_a),
                (port_b, token_b, session_b),
            ):
                names = _listed_tools(port, token, session)
                assert {"list_workdirs", "read_text_file"} <= names
                assert not names.intersection(
                    {
                        "submit_agent_task",
                        "get_agent_task",
                        "run_repo_action",
                        "commit_repo_changes",
                        "push_repo",
                    }
                )

            assert (
                _post(
                    port_a,
                    token_a,
                    {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
                    origin="http://untrusted.example.test",
                )[0]
                == 403
            )
            assert (
                _post(
                    port_b,
                    token_a,
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                )[0]
                == 401
            )
            assert (
                _post(
                    port_a,
                    token_a,
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
                    host=f"127.0.0.1:{port_b}",
                )[0]
                == 421
            )
            assert (
                _post(
                    port_b,
                    token_b,
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
                    session=session_a,
                )[0]
                == 404
            )

            signed_a = _client(first, key=keys_a["core-a"], validator=job_a.contains_pid)
            signed_b = _client(second, key=keys_b["core-a"], validator=job_b.contains_pid)
            assert _ping(signed_a) == {"status": "ok"}
            assert _ping(signed_b) == {"status": "ok"}

            # Restart only Core A. Both Bridge generations and Core B survive.
            core_a_pid = cores[0].pid
            core_b_pid = cores[1].pid
            bridge_a_pid = bridges[0].pid
            bridge_b_pid = bridges[1].pid
            cores[0].terminate()
            cores[0].wait(timeout=10)
            assert cores[1].poll() is None and cores[1].pid == core_b_pid
            assert bridges[0].poll() is None and bridges[0].pid == bridge_a_pid
            assert bridges[1].poll() is None and bridges[1].pid == bridge_b_pid
            assert _ping(signed_a) == {"status": "ok"}
            assert _ping(signed_b) == {"status": "ok"}

            cores.append(_launch_core(root_a, port=port_a, token=token_a))
            restarted_a = cores[-1]
            _wait_core(restarted_a, port_a)
            assert restarted_a.pid != core_a_pid
            assert (
                _post(
                    port_a,
                    token_a,
                    {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
                    session=session_a,
                )[0]
                == 404
            )
            new_session_a = _initialize(port_a, token_a)
            assert "first" in json.dumps(_workdirs(port_a, token_a, new_session_a))
            assert "second" in json.dumps(_workdirs(port_b, token_b, session_b))

            # Stop only Bridge A. Both Core HTTP listeners and Bridge B survive.
            request_graceful_shutdown(bridges[0])
            assert await_graceful_exit(bridges[0], timeout=10)
            assert bridges[0].returncode == 0
            assert restarted_a.poll() is None
            assert cores[1].poll() is None and cores[1].pid == core_b_pid
            assert bridges[1].poll() is None and bridges[1].pid == bridge_b_pid
            assert _ping(signed_b) == {"status": "ok"}
            assert "first" in json.dumps(_workdirs(port_a, token_a, new_session_a))
            assert "second" in json.dumps(_workdirs(port_b, token_b, session_b))
        finally:
            for core in cores:
                if core.poll() is None:
                    core.terminate()
                    try:
                        core.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        core.kill()
                        core.wait(timeout=5)
            for bridge in bridges:
                request_graceful_shutdown(bridge)
                if bridge.poll() is None:
                    await_graceful_exit(bridge, timeout=2)

    assert all(proc.poll() is not None for proc in (*cores, *bridges))
