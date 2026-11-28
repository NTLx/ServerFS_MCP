"""Native Streamable HTTP origin contract tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from serverfs_mcp.cli import build_parser
from serverfs_mcp.config import Settings
from serverfs_mcp.http_auth import StaticAPIKeyMiddleware
from serverfs_mcp.main import (
    create_server,
    create_streamable_http_app,
    log_startup,
    run_streamable_http,
    streamable_http_transport_security,
)
from serverfs_mcp.native_config import load_native_config
from serverfs_mcp.workdirs import WorkdirRegistry


def test_serve_defaults_to_stdio_and_http_is_explicit(tmp_path: Path) -> None:
    parser = build_parser()
    defaults = parser.parse_args(["serve", "--config", str(tmp_path / "serverfs.toml")])
    assert defaults.transport == "stdio"
    assert defaults.port == 8765
    assert defaults.path == "/mcp"
    http = parser.parse_args(
        ["serve", "--config", "serverfs.toml", "--transport", "streamable-http"]
    )
    assert http.transport == "streamable-http"
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["serve", "--config", "serverfs.toml", "--api-key", "must-not-be-an-argument"]
        )


@pytest.mark.parametrize("port", ["0", "65536", "nope"])
def test_http_port_is_bounded(port: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["serve", "--config", "serverfs.toml", "--transport", "streamable-http", "--port", port]
        )


def test_http_path_rejects_ambiguous_values() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve", "--config", "serverfs.toml", "--path", "//evil/mcp"])


def test_native_http_refuses_to_start_without_effective_auth() -> None:
    with pytest.raises(ValueError, match="requires API Key or OAuth"):
        run_streamable_http(None, Settings(http_host="127.0.0.1"), None)
    with pytest.raises(ValueError, match="must bind to 127.0.0.1"):
        run_streamable_http(
            None,
            Settings(http_host="0.0.0.0", http_api_key="test-key"),
            None,
        )


def _make_http_app(tmp_path: Path, monkeypatch):
    root = tmp_path / "workdir"
    root.mkdir()
    (root / "hello.txt").write_text("native HTTP works\n", encoding="utf-8")
    config = tmp_path / "serverfs.toml"
    config.write_text(
        "[[workdirs]]\nalias = 'repo'\npath = " + json.dumps(str(root)) + "\nread_only = true\n",
        encoding="utf-8",
    )
    workdirs, native_settings = load_native_config(config)
    # Linux env workdir configuration is deliberately present as a decoy. Native HTTP must
    # continue to use the TOML-owned registry passed to create_server.
    monkeypatch.setenv("SERVERFS_WORKDIR_01_ALIAS", "env-decoy")
    monkeypatch.setenv("SERVERFS_WORKDIR_01_PATH", str(tmp_path / "env-decoy"))
    registry = WorkdirRegistry(workdirs)
    settings = Settings(
        log_level=native_settings.log_level,
        http_host="127.0.0.1",
        http_port=8765,
        http_path="/mcp",
        http_allowed_hosts=("127.0.0.1:8765",),
        http_allowed_origins=("http://127.0.0.1:8765",),
        http_api_key="test-key",
    )
    mcp = create_server(settings, registry)
    app = StaticAPIKeyMiddleware(create_streamable_http_app(mcp, settings, registry), "test-key")
    return app, settings, registry, mcp


async def _asgi_post(app, path: str, headers: dict[str, str], value: dict):
    body = json.dumps(value).encode("utf-8")
    raw_headers = [(key.lower().encode(), item.encode()) for key, item in headers.items()]
    messages = []
    received = False

    async def receive():
        nonlocal received
        if received:
            await asyncio.Event().wait()
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8765),
        },
        receive,
        send,
    )
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    response_headers = dict(
        (key.decode().lower(), value.decode())
        for item in messages
        if item["type"] == "http.response.start"
        for key, value in item["headers"]
    )
    response_body = b"".join(
        item.get("body", b"") for item in messages if item["type"] == "http.response.body"
    )
    payload = None
    if response_body.startswith(b"{"):
        payload = json.loads(response_body)
    for line in response_body.decode("utf-8").splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[6:])
    return status, response_headers, payload, response_body


def test_native_http_initialize_tools_read_and_security(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    async def exercise() -> None:
        app, settings, registry, mcp = _make_http_app(tmp_path, monkeypatch)
        security = streamable_http_transport_security(settings)
        assert security.enable_dns_rebinding_protection is True
        assert security.allowed_hosts == ["127.0.0.1:8765"]
        assert security.allowed_origins == ["http://127.0.0.1:8765"]
        log_startup(settings, registry)
        assert "test-key" not in capsys.readouterr().err
        async with mcp.session_manager.run():
            await _exercise_http_requests(app)

    async def _exercise_http_requests(app) -> None:
        headers = {
            "Host": "127.0.0.1:8765",
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        no_auth, _, _, _ = await _asgi_post(
            app,
            "/mcp",
            {"Host": headers["Host"], "Content-Type": "application/json"},
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert no_auth == 401
        bad_host, _, _, _ = await _asgi_post(
            app,
            "/mcp",
            {**headers, "Host": "evil.example"},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
        )
        assert bad_host == 421
        bad_origin, _, _, _ = await _asgi_post(
            app,
            "/mcp",
            {**headers, "Origin": "http://evil.example"},
            {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}},
        )
        assert bad_origin == 403
        headers["Origin"] = "http://127.0.0.1:8765"
        initialize_status, initialize_headers, initialized, _ = await _asgi_post(
            app,
            "/mcp",
            headers,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "native-http-test", "version": "1"},
                },
            },
        )
        assert initialize_status == 200
        session_id = initialize_headers["mcp-session-id"]
        session_headers = {**headers, "mcp-session-id": session_id}
        session_headers.pop("Origin")
        initialized_status, _, _, _ = await _asgi_post(
            app,
            "/mcp",
            session_headers,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert initialized_status == 202
        listed_status, _, listed, listed_body = await _asgi_post(
            app,
            "/mcp",
            session_headers,
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
        )
        assert listed_status == 200
        assert listed is not None, listed_body
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert "read_text_file" in names
        assert "env-decoy" not in json.dumps(listed)
        read_status, _, read, read_body = await _asgi_post(
            app,
            "/mcp",
            session_headers,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "read_text_file",
                    "arguments": {"workdir": "repo", "path": "hello.txt"},
                },
            },
        )
        assert read_status == 200
        assert read is not None, read_body
        assert "native HTTP works" in json.dumps(read)
        assert initialized["result"]["serverInfo"]["name"] == "ServerFS"

    asyncio.run(exercise())
