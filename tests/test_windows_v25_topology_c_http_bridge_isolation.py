"""Topology C isolated Windows gate: two real Bridge jobs plus two HTTP MCP stacks.

The authenticated HTTP Core stacks are separate in-memory ASGI instances, not
two installed native Core processes or public connectors. Bridge workers are
real independent Windows subprocesses inside distinct named Jobs, using
temporary protected grants, pipes and read-only workdirs. No Agent provider.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path

import pytest

from serverfs_mcp.agent_lifecycle import (
    await_graceful_exit,
    bootstrap_frame_bytes,
    bridge_child_environment,
    request_graceful_shutdown,
    spawn_bridge,
)
from serverfs_mcp.config import Settings
from serverfs_mcp.http_auth import StaticAPIKeyMiddleware
from serverfs_mcp.main import create_server, create_streamable_http_app
from serverfs_mcp.windows_job import WindowsJob
from serverfs_mcp.windows_v25.bridge_v2_owner import wait_for_signed_bridge_v2_ready
from serverfs_mcp.workdirs import Workdir, WorkdirRegistry
from test_native_http import _asgi_post
from test_windows_v25_bridge_v2_real_worker import _test_installation
from test_windows_v25_two_independent_bridge_workers import _client, _ping

if sys.platform != "win32":
    pytest.skip(
        "Windows Bridge workers require native Jobs and Named Pipes", allow_module_level=True
    )


def _http_core(workdir: Path, *, port: int, token: str):
    registry = WorkdirRegistry(
        [Workdir(alias="Repo", root=workdir, description=workdir.parent.name, read_only=True)]
    )
    settings = Settings(
        http_host="127.0.0.1",
        http_port=port,
        http_path="/mcp",
        http_api_key=token,
        http_allowed_hosts=(f"127.0.0.1:{port}",),
        http_allowed_origins=(f"http://127.0.0.1:{port}",),
    )
    mcp = create_server(settings, registry)
    app = StaticAPIKeyMiddleware(create_streamable_http_app(mcp, settings, registry), token)
    return mcp, app


def _headers(port: int, token: str) -> dict[str, str]:
    return {
        "Host": f"127.0.0.1:{port}",
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


async def _initialize(app, headers: dict[str, str], *, ident: int) -> str:
    status, response_headers, payload, _ = await _asgi_post(
        app,
        "/mcp",
        headers,
        {
            "jsonrpc": "2.0",
            "id": ident,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "isolated-topology-c", "version": "1"},
            },
        },
    )
    assert status == 200
    assert payload["result"]["serverInfo"]["name"] == "ServerFS"
    session_id = response_headers["mcp-session-id"]
    status, _, _, _ = await _asgi_post(
        app,
        "/mcp",
        {**headers, "mcp-session-id": session_id},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert status == 202
    return session_id


async def _inventory(app, headers: dict[str, str], session: str, *, ident: int) -> dict:
    """Exercise real SDK/HTTP/MCP policy routing without native file backend."""
    status, _, payload, body = await _asgi_post(
        app,
        "/mcp",
        {**headers, "mcp-session-id": session},
        {
            "jsonrpc": "2.0",
            "id": ident,
            "method": "tools/call",
            "params": {"name": "list_workdirs", "arguments": {}},
        },
    )
    assert status == 200, body
    assert payload is not None
    assert payload["result"].get("isError") is not True
    return payload


def test_two_http_core_stacks_and_real_independent_bridge_jobs(tmp_path: Path) -> None:
    installed = []
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        plan, keys = _test_installation(directory)
        marker = directory / "read-only" / "marker.txt"
        marker.write_text("private-marker-" + name, encoding="utf-8")
        installed.append((plan, keys, marker.parent))
    (first, first_keys, root_a), (second, second_keys, root_b) = installed
    token_a, token_b = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    assert token_a != token_b
    port_a, port_b = 18981, 18982  # ASGI scope only; no TCP listeners are opened.
    mcp_a, app_a = _http_core(root_a, port=port_a, token=token_a)
    mcp_b, app_b = _http_core(root_b, port=port_b, token=token_b)

    workers = []
    with ExitStack() as stack:
        job_a = stack.enter_context(WindowsJob(first.job_name))
        job_b = stack.enter_context(WindowsJob(second.job_name))
        try:
            for plan, job in ((first, job_a), (second, job_b)):
                process = spawn_bridge(
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
                workers.append(process)
                assert process.stdin is not None
                bootstrap = bootstrap_frame_bytes(None, prior_bridge_execution_stopped=True)
                assert process.stdin.write(bootstrap) == len(bootstrap)
                process.stdin.flush()
                asyncio.run(
                    wait_for_signed_bridge_v2_ready(
                        plan, process, server_pid_validator=job.contains_pid, timeout=15.0
                    )
                )

            first_worker, second_worker = workers
            client_a = _client(first, key=first_keys["core-a"], validator=job_a.contains_pid)
            client_b = _client(second, key=second_keys["core-a"], validator=job_b.contains_pid)
            assert _ping(client_a) == {"status": "ok"}
            assert _ping(client_b) == {"status": "ok"}

            async def check_http() -> None:
                async with AsyncExitStack() as manager:
                    await manager.enter_async_context(mcp_a.session_manager.run())
                    await manager.enter_async_context(mcp_b.session_manager.run())
                    headers_a = _headers(port_a, token_a)
                    headers_b = _headers(port_b, token_b)
                    session_a = await _initialize(app_a, headers_a, ident=1)
                    session_b = await _initialize(app_b, headers_b, ident=2)
                    assert session_a != session_b

                    payload_a = await _inventory(app_a, headers_a, session_a, ident=3)
                    payload_b = await _inventory(app_b, headers_b, session_b, ident=4)
                    assert "first" in json.dumps(payload_a)
                    assert "second" in json.dumps(payload_b)
                    assert "second" not in json.dumps(payload_a)
                    assert "first" not in json.dumps(payload_b)
                    assert "read-only" in json.dumps(payload_a)
                    assert "read-only" in json.dumps(payload_b)

                    denied, _, _, _ = await _asgi_post(
                        app_b,
                        "/mcp",
                        {**headers_b, "Authorization": "Bearer " + token_a},
                        {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
                    )
                    assert denied == 401
                    wrong_host, _, _, _ = await _asgi_post(
                        app_a,
                        "/mcp",
                        {**headers_a, "Host": f"127.0.0.1:{port_b}"},
                        {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
                    )
                    assert wrong_host == 421
                    foreign_session, _, _, _ = await _asgi_post(
                        app_b,
                        "/mcp",
                        {**headers_b, "mcp-session-id": session_a},
                        {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
                    )
                    assert foreign_session == 404

                    request_graceful_shutdown(first_worker)
                    assert await_graceful_exit(first_worker, timeout=10)
                    assert first_worker.returncode == 0
                    assert second_worker.poll() is None
                    assert job_b.contains_pid(second_worker.pid)
                    assert await client_b.call("bridge.ping", {"workdir": "Repo"}) == {
                        "status": "ok"
                    }
                    # File-only HTTP tools continue when an unrelated Bridge exits.
                    assert "first" in json.dumps(
                        await _inventory(app_a, headers_a, session_a, ident=8)
                    )
                    assert "second" in json.dumps(
                        await _inventory(app_b, headers_b, session_b, ident=9)
                    )

            asyncio.run(check_http())
        finally:
            for process in workers:
                request_graceful_shutdown(process)
                if process.poll() is None:
                    await_graceful_exit(process, timeout=2)
    assert all(process.poll() is not None for process in workers)
