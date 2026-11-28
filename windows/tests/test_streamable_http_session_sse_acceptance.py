"""Opt-in loopback acceptance for authenticated stateful Streamable HTTP sessions."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from contextlib import contextmanager

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from serverfs_mcp.config import Settings
from serverfs_mcp.http_auth import StaticAPIKeyMiddleware
from serverfs_mcp.main import create_server, run_http_server, streamable_http_transport_security
from serverfs_mcp.workdirs import WorkdirRegistry

RUN_ACCEPTANCE = os.environ.get("SERVERFS_RUN_LOCAL_HTTP_SESSION_SSE_ACCEPTANCE") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_ACCEPTANCE,
    reason=("enable SERVERFS_RUN_LOCAL_HTTP_SESSION_SSE_ACCEPTANCE=1 for the 75s loopback gate"),
)

TEST_API_KEY = "streamable-http-loopback-sse-test-key-v1"
SSE_HOLD_SECONDS = 75
SERVER_START_TIMEOUT_SECONDS = 5
SERVER_STOP_TIMEOUT_SECONDS = 8
REQUEST_TIMEOUT_SECONDS = 3


class _SSEObserver:
    def __init__(self, app) -> None:
        self._app = app
        self._lock = threading.Lock()
        self.get_connections: list[dict[str, float | int | None]] = []
        self.session_ids: list[str] = []

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        is_get = scope["method"] == "GET"
        connection_index: int | None = None

        async def observe_send(message):
            nonlocal connection_index
            now = time.monotonic()
            if message["type"] == "http.response.start":
                headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
                with self._lock:
                    session_id = headers.get("mcp-session-id")
                    if session_id:
                        self.session_ids.append(session_id)
                    if is_get:
                        connection_index = len(self.get_connections)
                        self.get_connections.append(
                            {
                                "started_at": now,
                                "body_messages": 0,
                                "finished_at": None,
                                "status": message["status"],
                            }
                        )
            elif message["type"] == "http.response.body" and is_get:
                with self._lock:
                    if connection_index is not None:
                        connection = self.get_connections[connection_index]
                        connection["body_messages"] = int(connection["body_messages"] or 0) + 1
                        if not message.get("more_body", False):
                            connection["finished_at"] = now
            await send(message)

        await self._app(scope, receive, observe_send)

    def snapshot(self) -> tuple[list[dict[str, float | int | None]], list[str]]:
        with self._lock:
            return [dict(connection) for connection in self.get_connections], list(self.session_ids)


def _listener(port: int = 0) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    return listener


@contextmanager
def _serve(listener: socket.socket, observer: _SSEObserver):
    server = uvicorn.Server(
        uvicorn.Config(observer, log_config=None, access_log=False, lifespan="on")
    )
    thread = threading.Thread(
        target=lambda: run_http_server(server.serve(sockets=[listener])), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)
        listener.close()
        raise RuntimeError("loopback Streamable HTTP server did not start")
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("loopback Streamable HTTP server did not stop")


def _app(port: int):
    settings = Settings(
        http_host="127.0.0.1",
        http_port=port,
        http_allowed_hosts=(f"127.0.0.1:{port}",),
        http_api_key=TEST_API_KEY,
    )
    mcp = create_server(settings, WorkdirRegistry([]))
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=False,
        transport_security=streamable_http_transport_security(settings),
        host=settings.http_host,
    )
    return StaticAPIKeyMiddleware(app, TEST_API_KEY)


async def _initialize(
    url: str,
    observer: _SSEObserver,
    initialized: asyncio.Future[tuple[str, str]],
    release: asyncio.Event,
) -> None:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        trust_env=False,
        timeout=httpx2.Timeout(REQUEST_TIMEOUT_SECONDS, read=None),
    ) as http_client:
        transport = streamable_http_client(url, http_client=http_client)
        async with Client(transport, mode="legacy", raise_exceptions=True) as client:
            tools = await client.list_tools()
            assert any(tool.name == "list_workdirs" for tool in tools.tools)
            session_ids = observer.snapshot()[1]
            assert session_ids
            initialized.set_result((str(client.protocol_version), session_ids[0]))
            # Keep the SDK client and its SDK-managed GET SSE stream active until cancelled.
            await release.wait()


async def _status(url: str, *, authorization: str | None, session_id: str) -> int:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Mcp-Session-Id": session_id,
    }
    if authorization is not None:
        headers["Authorization"] = authorization
    async with httpx2.AsyncClient(trust_env=False, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 41, "method": "tools/list", "params": {}},
        )
        return response.status_code


async def _wait_for_get(observer: _SSEObserver) -> None:
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        connections, _ = observer.snapshot()
        if connections:
            assert connections[-1]["status"] == 200
            assert connections[-1]["finished_at"] is None
            return
        await asyncio.sleep(0.01)
    raise AssertionError("SDK client did not establish its GET text/event-stream connection")


def test_authenticated_stateful_session_sse_hold_restart_and_reconnect() -> None:
    listener = _listener()
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    observer = _SSEObserver(_app(port))
    old_session_id: str | None = None

    with _serve(listener, observer) as live_url:
        assert live_url == url
        unauthenticated_status = asyncio.run(_status(url, authorization=None, session_id="missing"))
        wrong_key_status = asyncio.run(
            _status(url, authorization="Bearer wrong-loopback-test-key", session_id="missing")
        )
        assert unauthenticated_status == 401
        assert wrong_key_status == 401

        async def hold_sdk_session() -> tuple[str, str]:
            initialized: asyncio.Future[tuple[str, str]] = (
                asyncio.get_running_loop().create_future()
            )
            release = asyncio.Event()
            task = asyncio.create_task(_initialize(url, observer, initialized, release))
            try:
                protocol, session_id = await asyncio.wait_for(
                    initialized, SERVER_START_TIMEOUT_SECONDS
                )
                await _wait_for_get(observer)
                await asyncio.sleep(SSE_HOLD_SECONDS)
                connections, _ = observer.snapshot()
                assert len(connections) == 1, (
                    "SDK client reconnected its GET stream during the hold"
                )
                connection = connections[0]
                assert connection["finished_at"] is None and connection["status"] == 200
                assert time.monotonic() - float(connection["started_at"]) > 65
                body_messages = int(connection["body_messages"] or 0)
                assert body_messages >= 4, "expected periodic SSE keepalive body messages"
                valid_status = await _status(
                    url,
                    authorization=f"Bearer {TEST_API_KEY}",
                    session_id=session_id,
                )
                assert valid_status == 200
                return protocol, session_id
            finally:
                release.set()
                await asyncio.wait_for(task, SERVER_STOP_TIMEOUT_SECONDS)

        protocol, old_session_id = asyncio.run(hold_sdk_session())
        assert protocol == "2025-11-25"
        _, session_ids = observer.snapshot()
        assert old_session_id in session_ids

    # A restarted isolated server has fresh in-memory session state. Do not test replay.
    restarted_observer = _SSEObserver(_app(port))
    with _serve(_listener(port), restarted_observer) as restarted_url:
        stale_status = asyncio.run(
            _status(
                restarted_url,
                authorization=f"Bearer {TEST_API_KEY}",
                session_id=old_session_id,
            )
        )
        assert stale_status == 404

        async def fresh_sdk_session() -> tuple[str, str]:
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
                trust_env=False,
                timeout=httpx2.Timeout(REQUEST_TIMEOUT_SECONDS, read=None),
            ) as http_client:
                transport = streamable_http_client(restarted_url, http_client=http_client)
                async with Client(transport, mode="legacy", raise_exceptions=True) as client:
                    tools = await client.list_tools()
                    assert any(tool.name == "list_workdirs" for tool in tools.tools)
                    return str(client.protocol_version), restarted_observer.snapshot()[1][0]

        fresh_protocol, fresh_session_id = asyncio.run(fresh_sdk_session())
        assert fresh_protocol == "2025-11-25"
        assert fresh_session_id
        assert fresh_session_id != old_session_id
