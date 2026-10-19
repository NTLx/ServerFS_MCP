"""Static API Key end-to-end acceptance over a real loopback HTTP socket."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Any

import uvicorn

from helpers import read_write, registry_for
from serverfs_mcp.config import Settings
from serverfs_mcp.http_auth import StaticAPIKeyMiddleware
from serverfs_mcp.main import create_server, streamable_http_transport_security

API_KEY = "api-key-e2e-sentinel-9F33A53D"
_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _bound_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener


@contextmanager
def _serve(app, listener: socket.socket):
    config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError("test HTTP server did not start")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("test HTTP server did not stop")


def _http(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=headers or {},
    )
    try:
        with _HTTP.open(request, timeout=3) as response:
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            {key.lower(): value for key, value in exc.headers.items()},
            exc.read(),
        )


def _json_rpc(
    url: str,
    *,
    method: str,
    params: dict[str, Any] | None = None,
    authorization: str | None = None,
    session_id: str | None = None,
    request_id: int = 1,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if authorization is not None:
        headers["Authorization"] = authorization
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    status, response_headers, body = _http(
        url,
        headers=headers,
        body=json.dumps(message).encode("utf-8"),
    )
    content_type = response_headers.get("content-type", "")
    payload = json.loads(body) if body and content_type.startswith("application/json") else None
    return status, response_headers, payload


def test_static_api_key_protects_real_streamable_http_surface(workdir) -> None:
    listener = _bound_listener()
    port = listener.getsockname()[1]
    resource_url = f"http://127.0.0.1:{port}/mcp"
    writable = read_write(workdir)
    settings = Settings(
        http_host="127.0.0.1",
        http_port=port,
        http_allowed_hosts=(f"127.0.0.1:{port}",),
        http_api_key=API_KEY,
    )
    mcp = create_server(settings, registry_for(writable))
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=False,
        transport_security=streamable_http_transport_security(settings),
        host=settings.http_host,
    )
    protected_app = StaticAPIKeyMiddleware(app, API_KEY)
    initialize_params = {
        "protocolVersion": "2026-07-28",
        "capabilities": {},
        "clientInfo": {"name": "api-key-e2e", "version": "1"},
    }

    with _serve(protected_app, listener):
        status, headers, payload = _json_rpc(
            resource_url,
            method="initialize",
            params=initialize_params,
        )
        assert status == 401
        assert "www-authenticate" not in headers
        assert payload is None

        status, _, _ = _json_rpc(
            resource_url,
            method="initialize",
            params=initialize_params,
            authorization="Bearer wrong-key",
        )
        assert status == 401

        status, headers, payload = _json_rpc(
            resource_url,
            method="initialize",
            params=initialize_params,
            authorization=f"Bearer {API_KEY}",
        )
        assert status == 200
        assert payload is not None and "error" not in payload
        session_id = headers["mcp-session-id"]

        status, _, payload = _json_rpc(
            resource_url,
            method="tools/list",
            authorization=f"Bearer {API_KEY}",
            session_id=session_id,
            request_id=2,
        )
        assert status == 200
        assert payload is not None and "error" not in payload
        assert any(tool["name"] == "list_workdirs" for tool in payload["result"]["tools"])

        query_url = f"{resource_url}?{urllib.parse.urlencode({'key': API_KEY})}"
        status, _, payload = _json_rpc(
            query_url,
            method="initialize",
            params=initialize_params,
            request_id=3,
        )
        assert status == 200
        assert payload is not None and "error" not in payload
