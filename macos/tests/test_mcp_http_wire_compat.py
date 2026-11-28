"""MCP 2.3.0 HTTP wire compatibility over short lived loopback servers."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import WorkdirRegistry


class _Recorder:
    def __init__(self, app) -> None:
        self.app = app
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        record = {
            "method": scope["method"],
            "rpc_method": None,
            "headers": {
                key: headers[key]
                for key in (
                    "mcp-session-id",
                    "mcp-protocol-version",
                    "mcp-method",
                    "mcp-name",
                )
                if key in headers
            },
            "params": {},
        }
        chunks: list[bytes] = []

        async def recording_receive():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
            return message

        async def recording_send(message):
            if message["type"] == "http.response.start":
                response_headers = {
                    key.decode().lower(): value.decode()
                    for key, value in message.get("headers", [])
                }
                if "mcp-session-id" in response_headers:
                    record["response_session_id"] = response_headers["mcp-session-id"]
                body = b"".join(chunks)
                try:
                    payload = json.loads(body) if body else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                rpc_message = payload if isinstance(payload, dict) else {}
                record["rpc_method"] = rpc_message.get("method")
                params = (
                    rpc_message.get("params") if isinstance(rpc_message.get("params"), dict) else {}
                )
                if "_meta" in params:
                    record["params"]["_meta"] = params["_meta"]
                with self._lock:
                    self.requests.append(record)
            await send(message)

        await self.app(scope, recording_receive, recording_send)


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener


@contextmanager
def _serve(app):
    listener = _listener()
    recorder = _Recorder(app)
    server = uvicorn.Server(
        uvicorn.Config(recorder, log_config=None, access_log=False, lifespan="on")
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener.close()
        raise RuntimeError("test HTTP server did not start")
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp", recorder
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("test HTTP server did not stop")


def _client_roundtrip(url: str, mode: str):
    async def run():
        async with httpx2.AsyncClient(trust_env=False) as http_client:
            transport = streamable_http_client(url, http_client=http_client)
            async with Client(transport, mode=mode, raise_exceptions=True) as client:
                tools = await client.list_tools()
                result = await client.call_tool("list_workdirs", {})
                return str(client.protocol_version), {tool.name for tool in tools.tools}, result

    return asyncio.run(run())


def _stateful_app():
    server = create_server(Settings(), WorkdirRegistry([]))
    return server.streamable_http_app()


def _raw_post(url: str, *, headers: dict[str, str], body: bytes):
    async def run():
        async with httpx2.AsyncClient(trust_env=False) as client:
            return await client.post(url, headers=headers, content=body)

    return asyncio.run(run())


def test_raw_http_rejects_malformed_json() -> None:
    with _serve(_stateful_app()) as (url, _):
        response = _raw_post(
            url,
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            body=b"{",
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_raw_http_rejects_invalid_jsonrpc_shape() -> None:
    with _serve(_stateful_app()) as (url, _):
        response = _raw_post(
            url,
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            body=b'{"jsonrpc":"2.0","method":"tools/list"}',
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


def test_raw_http_rejects_post_without_required_accept_types() -> None:
    with _serve(_stateful_app()) as (url, _):
        response = _raw_post(
            url,
            headers={"content-type": "application/json", "accept": "application/json"},
            body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        )

    assert response.status_code == 406
    message = response.json()["error"]["message"]
    assert "must accept both application/json and text/event-stream" in message


def test_stateful_legacy_client_negotiates_and_closes_session() -> None:
    with _serve(_stateful_app()) as (url, recorder):
        protocol, tools, result = _client_roundtrip(url, "legacy")

    assert protocol == "2025-11-25"
    assert "list_workdirs" in tools
    assert result.structured_content == {"workdirs": []}
    requests = recorder.requests
    assert requests[0]["rpc_method"] == "initialize"
    session_id = requests[0].get("response_session_id")
    assert session_id
    assert any(
        request["rpc_method"] == "tools/list"
        and request["headers"].get("mcp-session-id") == session_id
        for request in requests
    )
    assert any(
        request["rpc_method"] == "tools/call"
        and request["headers"].get("mcp-session-id") == session_id
        for request in requests
    )
    assert any(
        request["method"] == "DELETE" and request["headers"].get("mcp-session-id") == session_id
        for request in requests
    )


def test_stateful_modern_client_uses_per_request_wire_metadata() -> None:
    with _serve(_stateful_app()) as (url, recorder):
        protocol, tools, result = _client_roundtrip(url, "2026-07-28")

    assert protocol == "2026-07-28"
    assert "list_workdirs" in tools
    assert result.structured_content == {"workdirs": []}
    requests = recorder.requests
    assert all(request["rpc_method"] != "initialize" for request in requests)
    assert not any("mcp-session-id" in request["headers"] for request in requests)
    assert any(request["rpc_method"] == "tools/list" for request in requests)
    call = next(request for request in requests if request["rpc_method"] == "tools/call")
    assert call["headers"].get("mcp-protocol-version") == "2026-07-28"
    assert call["headers"].get("mcp-method") == "tools/call"
    assert call["headers"].get("mcp-name") == "list_workdirs"
    metadata = call["params"]["_meta"]
    assert metadata["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert "name" in metadata["io.modelcontextprotocol/clientInfo"]
    assert isinstance(metadata["io.modelcontextprotocol/clientCapabilities"], dict)


def test_stateful_auto_client_discovers_then_uses_modern_protocol() -> None:
    with _serve(_stateful_app()) as (url, recorder):
        protocol, tools, result = _client_roundtrip(url, "auto")

    assert protocol == "2026-07-28"
    assert "list_workdirs" in tools
    assert result.structured_content == {"workdirs": []}
    requests = recorder.requests
    assert requests[0]["rpc_method"] == "server/discover"
    assert any(request["rpc_method"] == "tools/list" for request in requests)
    assert any(request["rpc_method"] == "tools/call" for request in requests)
    assert all(request["rpc_method"] != "initialize" for request in requests)


def test_stateless_legacy_client_calls_tool_without_session_lifecycle() -> None:
    server = create_server(Settings(), WorkdirRegistry([]))
    app = server.streamable_http_app(stateless_http=True)
    with _serve(app) as (url, recorder):
        protocol, tools, result = _client_roundtrip(url, "legacy")

    assert protocol == "2025-11-25"
    assert "list_workdirs" in tools
    assert result.structured_content == {"workdirs": []}
    assert any(request["rpc_method"] == "tools/call" for request in recorder.requests)
    assert all("mcp-session-id" not in request["headers"] for request in recorder.requests)
    assert all(request["method"] != "DELETE" for request in recorder.requests)
