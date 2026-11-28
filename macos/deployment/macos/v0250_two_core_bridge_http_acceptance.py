"""Real loopback dual Core -> dual owned Bridge IPC smoke (Fake Adapter only).

No launchd registration. Uses disposable Core HTTP sockets and two supervised
Bridge subprocesses, isolated private data/sockets/leases, and synthetic API
keys. Every provider is disabled except the Bridge's inbuilt fake test adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from v0250_two_owned_bridges_ipc_acceptance import (
    PROTECTED,
    _identity,
    _prepare,
    _shutdown,
    _start,
)

from serverfs_mcp.agent_client import AgentBridgeClient
from serverfs_mcp.config import Settings
from serverfs_mcp.darwin_service import _launchd_job_pid
from serverfs_mcp.http_auth import StaticAPIKeyMiddleware
from serverfs_mcp.main import create_server, streamable_http_transport_security
from serverfs_mcp.workdirs import Workdir, WorkdirRegistry


def _listener() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


@contextmanager
def _core(workdir: Path, alias: str, bridge_socket: Path, lock_dir: Path, key: str):
    listener = _listener()
    port = listener.getsockname()[1]
    settings = Settings(
        http_host="127.0.0.1",
        http_port=port,
        http_allowed_hosts=(f"127.0.0.1:{port}",),
        http_api_key=key,
        agent_bridge_enabled=True,
        agent_bridge_socket=str(bridge_socket),
        agent_lock_dir=str(lock_dir),
    )
    registry = WorkdirRegistry(
        [
            Workdir(
                alias=alias,
                root=workdir,
                description="isolated owned-Bridge synthetic Core",
                read_only=False,
                agent_mode="workspace-write",
                agent_runtimes=frozenset({"codex"}),
            )
        ]
    )
    mcp = create_server(
        settings,
        registry,
        agent_client=AgentBridgeClient(bridge_socket, timeout_seconds=5.0),
    )
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=False,
        transport_security=streamable_http_transport_security(settings),
        host="127.0.0.1",
    )
    server = uvicorn.Server(
        uvicorn.Config(
            StaticAPIKeyMiddleware(app, key),
            log_config=None,
            access_log=False,
            lifespan="on",
        )
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])),
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 8
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("synthetic Core HTTP endpoint failed to become ready")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=7)
        if thread.is_alive():
            raise RuntimeError("synthetic Core HTTP server did not terminate")


async def _fake_task(client: AgentBridgeClient, alias: str) -> str:
    result = await client.call(
        "task.submit",
        {
            "runtime": "fake",
            "workdir": alias,
            "path": "",
            "profile": "review",
            "prompt": f"complete:{alias}",
        },
    )
    identifier = result["task_id"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        details = await client.call("task.get", {"task_id": identifier})
        if details["status"] == "succeeded":
            assert details["final_response"] == alias
            return identifier
        if details["status"] in {"failed", "cancelled", "interrupted"}:
            raise RuntimeError("fake Bridge task was not successful")
        await asyncio.sleep(0.05)
    raise RuntimeError("fake Bridge task did not complete")


async def _http_roundtrip(
    url: str, api_key: str, alias: str, own_task: str, foreign_task: str
) -> None:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=8,
        trust_env=False,
    ) as http_client:
        transport = streamable_http_client(url, http_client=http_client, terminate_on_close=False)
        async with Client(transport, mode="2026-07-28", raise_exceptions=True) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"list_workdirs", "get_agent_task", "list_agent_runtimes"} <= names
            roots = await client.call_tool("list_workdirs", {})
            assert roots.structured_content is not None
            assert {item["alias"] for item in roots.structured_content["workdirs"]} == {alias}

            # This Core is wired to its own Bridge; a fake task submitted via
            # that Bridge's IPC can be read back through the authenticated MCP.
            reply = await client.call_tool("get_agent_task", {"task_id": own_task})
            assert not reply.is_error
            assert reply.structured_content is not None
            assert reply.structured_content["task_id"] == own_task
            assert reply.structured_content["final_response"] == alias

            try:
                foreign = await client.call_tool("get_agent_task", {"task_id": foreign_task})
            except Exception:
                pass
            else:
                assert foreign.is_error or (
                    foreign.structured_content is None
                    or foreign.structured_content.get("task_id") != foreign_task
                ), "a Core read the other Bridge's task"


async def _probe_http(urls: list[str], keys: list[str], owners, resources) -> int:
    clients = [
        AgentBridgeClient(resource.socket_path, timeout_seconds=5.0) for resource in resources
    ]
    tasks = await asyncio.gather(
        *(
            _fake_task(client, owner.instance_id)
            for client, owner in zip(clients, owners, strict=True)
        )
    )
    for index, url in enumerate(urls):
        with httpx2.Client(trust_env=False, timeout=3) as client:
            unauth = client.get(url)
            wrong_key = client.get(url, headers={"Authorization": f"Bearer {keys[1 - index]}"})
        assert unauth.status_code == 401
        assert wrong_key.status_code == 401

    await asyncio.gather(
        *(
            _http_roundtrip(
                urls[index],
                keys[index],
                owners[index].instance_id,
                tasks[index],
                tasks[1 - index],
            )
            for index in range(2)
        )
    )
    return len(tasks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        print("NOT_EXECUTED: explicit --execute required")
        return 2
    baseline = tuple(_launchd_job_pid(label) for label in PROTECTED)
    if any(pid is None for pid in baseline):
        raise RuntimeError("production baseline incomplete; no isolated experiment")

    marker = secrets.token_hex(4)
    owners = (_identity(f"v25a-{marker}"), _identity(f"v25b-{marker}"))
    processes: list[subprocess.Popen[bytes]] = []
    socket_parents: list[Path] = []
    count = 0
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="serverfs-v25-core-bridge-") as temp:
        home = Path(temp).resolve()
        try:
            prepared = [_prepare(home, owner) for owner in owners]
            resources = [item[0] for item in prepared]
            socket_parents = [resource.socket_path.parent for resource in resources]
            for index, resource in enumerate(resources):
                processes.append(_start(resource, home / f"bridge-{index}.stderr.log"))
            keys = [f"synthetic-{secrets.token_hex(16)}" for _ in owners]
            with ExitStack() as stack:
                urls = [
                    stack.enter_context(
                        _core(
                            prepared[index][1],
                            owners[index].instance_id,
                            resources[index].socket_path,
                            resources[index].lock_dir,
                            keys[index],
                        )
                    )
                    for index in range(2)
                ]
                count = asyncio.run(_probe_http(urls, keys, owners, resources))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            for process in reversed(processes):
                _shutdown(process)
            for parent in socket_parents:
                if parent.exists():
                    if any(parent.iterdir()):
                        errors.append("synthetic Bridge socket remained after shutdown")
                    else:
                        parent.rmdir()
            preserved = tuple(_launchd_job_pid(label) for label in PROTECTED) == baseline
            if not preserved:
                errors.append("production PID baseline changed")
        payload = {
            "result": "PASS" if count == 2 and not errors else "NOT_ACCEPTED",
            "cores": 2,
            "bridges": len(processes),
            "fake_tasks_read_through_mcp": count,
            "production_pids_unchanged": preserved,
            "owned_sockets_removed": all(not path.exists() for path in socket_parents),
            "errors": errors,
        }
        print(json.dumps(payload, sort_keys=True))
    return 0 if count == 2 and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
