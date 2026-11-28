"""Windows topology A public-Host contract for multiple Cloudflare routes.

Core v25 deliberately accepts only one canonical local Host plus loopback.
Two DIFFERENT external Tunnel routes must independently rewrite the origin
HTTP Host to that canonical value. Test the real in-memory HTTP MCP/Host
security stack, without real Cloudflare, DNS, credentials, TCP or Scheduler.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from serverfs_mcp.config import Settings
from serverfs_mcp.http_auth import StaticAPIKeyMiddleware
from serverfs_mcp.main import create_server, create_streamable_http_app
from serverfs_mcp.windows_v25 import runtime
from serverfs_mcp.windows_v25.spec import Manifest
from serverfs_mcp.workdirs import Workdir, WorkdirRegistry
from test_native_http import _asgi_post

_CANONICAL = "core-a.example.test"
_EXTERNAL = ("route-a.example.test", "route-b.example.test")
_PORT = 18879
_KEY = "fixture-secret-never-used-outside-test"


def _http_core(tmp_path: Path):
    root = tmp_path / "read-only"
    root.mkdir()
    (root / "marker.txt").write_text("not-public", encoding="utf-8")
    registry = WorkdirRegistry(
        [Workdir(alias="Repo", root=root, description="local", read_only=True)]
    )
    settings = Settings(
        http_host="127.0.0.1",
        http_port=_PORT,
        http_path="/mcp",
        http_api_key=_KEY,
        http_allowed_hosts=(_CANONICAL, f"127.0.0.1:{_PORT}"),
        http_allowed_origins=(),
    )
    mcp = create_server(settings, registry)
    app = StaticAPIKeyMiddleware(create_streamable_http_app(mcp, settings, registry), _KEY)
    return mcp, app


def _headers(external: str, *, rewritten: bool = True) -> dict[str, str]:
    return {
        "Host": _CANONICAL if rewritten else external,
        "X-Forwarded-Host": external,
        "Authorization": "Bearer " + _KEY,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _init(ident: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "topology-a-private", "version": "1"},
        },
    }


def test_two_public_routes_need_local_host_rewrite_and_share_only_one_auth_core(
    tmp_path: Path,
) -> None:
    async def check() -> None:
        mcp, app = _http_core(tmp_path)
        sessions = []
        async with mcp.session_manager.run():
            for i, public in enumerate(_EXTERNAL):
                # Cloudflare route originRequest.httpHostHeader = canonical.
                status, reply_headers, body, _ = await _asgi_post(
                    app, "/mcp", _headers(public), _init(i + 1)
                )
                assert status == 200
                assert body["result"]["serverInfo"]["name"] == "ServerFS"
                session = reply_headers["mcp-session-id"]
                sessions.append(session)
                initialized, _, _, _ = await _asgi_post(
                    app,
                    "/mcp",
                    {**_headers(public), "mcp-session-id": session},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                assert initialized == 202
                code, _, payload, _ = await _asgi_post(
                    app,
                    "/mcp",
                    {**_headers(public), "mcp-session-id": session},
                    {
                        "jsonrpc": "2.0",
                        "id": 20 + i,
                        "method": "tools/call",
                        "params": {"name": "list_workdirs", "arguments": {}},
                    },
                )
                assert code == 200
                assert payload["result"].get("isError") is not True

            assert len(set(sessions)) == len(_EXTERNAL)

            for i, public in enumerate(_EXTERNAL):
                denied_host, _, _, _ = await _asgi_post(
                    app, "/mcp", _headers(public, rewritten=False), _init(30 + i)
                )
                assert denied_host == 421
                rejected_auth, _, _, _ = await _asgi_post(
                    app,
                    "/mcp",
                    {**_headers(public), "Authorization": "Bearer invalid"},
                    _init(40 + i),
                )
                assert rejected_auth == 401
                invalid_origin, _, _, _ = await _asgi_post(
                    app,
                    "/mcp",
                    {**_headers(public), "Origin": "https://evil.example"},
                    _init(50 + i),
                )
                assert invalid_origin != 200

    asyncio.run(check())


def test_v25_core_configuration_fails_closed_on_public_hostname_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import windows_service

    core = Manifest.parse(
        {
            "schema": 2,
            "instance_id": "host-contract-a",
            "component": "core",
            "sid": "S-1-5-21-111-222-333-1001",
            "pythonw": str(tmp_path / "envs/core/Scripts/pythonw.exe"),
            "settings": {
                "config": str(tmp_path / "policy.toml"),
                "auth_file": str(tmp_path / "private/core.env"),
                "port": _PORT,
                "hostname": _CANONICAL,
            },
        }
    )
    monkeypatch.setattr(runtime, "_roots", lambda config: (config, []))
    monkeypatch.setattr(runtime, "_private_input", lambda path, roots, sid: path)
    values = {
        "SERVERFS_HTTP_ALLOWED_HOSTS": f"{_CANONICAL},127.0.0.1:{_PORT}",
        "SERVERFS_HTTP_API_KEY": _KEY,
    }
    monkeypatch.setattr(windows_service, "_read_env_file", lambda path: dict(values))
    path, parsed = runtime._core_configuration(core)
    assert path == Path(core.settings["config"])
    assert parsed == values

    for other in _EXTERNAL:
        values["SERVERFS_HTTP_ALLOWED_HOSTS"] = f"{_CANONICAL},127.0.0.1:{_PORT},{other}"
        with pytest.raises(runtime.RuntimeErrorV25, match="Core HTTP policy cannot be validated"):
            runtime._core_configuration(core)
    values["SERVERFS_HTTP_ALLOWED_HOSTS"] = f"127.0.0.1:{_PORT},route-a.example.test"
    with pytest.raises(runtime.RuntimeErrorV25, match="Core HTTP policy cannot be validated"):
        runtime._core_configuration(core)
