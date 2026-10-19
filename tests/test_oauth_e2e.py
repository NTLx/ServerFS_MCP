"""Protocol-level OAuth end-to-end acceptance over real loopback HTTP sockets."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Any

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull

from helpers import read_write, registry_for
from serverfs_mcp.auth_config import AuthServiceSettings
from serverfs_mcp.auth_server import create_auth_app
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server, streamable_http_transport_security

CLIENT_ID = "oauth-e2e-client"
CALLBACK = "http://127.0.0.1:19876/callback"
INTROSPECTION_SECRET = "oauth-e2e-internal-secret"
CODE_VERIFIER = "v" * 64
CODE_CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode()).digest()).decode().rstrip("=")
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _http(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 3.0,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=headers or {},
    )
    try:
        with _HTTP.open(request, timeout=timeout) as response:
            status = response.status
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        payload = exc.read()
    return status, response_headers, payload


def _form(url: str, fields: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    return _http(
        url,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode(fields).encode("utf-8"),
    )


def _bound_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener


@contextmanager
def _serve(app, listener: socket.socket):
    config = uvicorn.Config(
        app,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
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


def _json_rpc(
    resource_url: str,
    *,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    request_id: int | None = 1,
    session_id: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    status, response_headers, body = _http(
        resource_url,
        method="POST",
        headers=headers,
        body=json.dumps(message).encode("utf-8"),
    )
    payload = json.loads(body) if body else None
    return status, response_headers, payload


def _tool_call(
    resource_url: str,
    token: str,
    session_id: str,
    request_id: int,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    status, _, payload = _json_rpc(
        resource_url,
        token=token,
        session_id=session_id,
        method="tools/call",
        params={"name": name, "arguments": arguments},
        request_id=request_id,
    )
    assert status == 200
    assert payload is not None and "error" not in payload
    result = payload["result"]
    assert result.get("isError") is not True
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    return json.loads(result["content"][0]["text"])


def test_direct_oauth_http_flow_reaches_real_mcp_surface(tmp_path, workdir) -> None:
    auth_listener = _bound_listener()
    resource_listener = _bound_listener()
    auth_port = auth_listener.getsockname()[1]
    resource_port = resource_listener.getsockname()[1]
    issuer = f"http://127.0.0.1:{auth_port}"
    resource_url = f"http://127.0.0.1:{resource_port}/mcp"

    auth_settings = AuthServiceSettings(
        oauth_enabled=True,
        public_url=issuer,
        resource_url=resource_url,
        state_dir=str(tmp_path / "auth-state"),
        allowed_principals=("github:123",),
        introspection_secret=INTROSPECTION_SECRET,
    )
    auth_app = create_auth_app(auth_settings, test_identity="github:123")
    client = OAuthClientInformationFull(
        client_id=CLIENT_ID,
        redirect_uris=[CALLBACK],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        scope="serverfs:access",
    )
    asyncio.run(auth_app.state.oauth_provider.register_client(client))

    writable = read_write(workdir)
    sample = writable.container_path / "sample.txt"
    sample.write_text("content\n", encoding="utf-8")
    resource_settings = Settings(
        http_host="127.0.0.1",
        http_port=resource_port,
        http_allowed_hosts=(f"127.0.0.1:{resource_port}",),
        oauth_enabled=True,
        oauth_resource_url=resource_url,
        oauth_issuer_url=issuer,
        oauth_required_scopes=("serverfs:access",),
        oauth_introspection_url=f"{issuer}/internal/introspect",
        oauth_introspection_secret=INTROSPECTION_SECRET,
        oauth_introspection_timeout_seconds=2,
    )
    mcp = create_server(resource_settings, registry_for(writable))
    resource_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=False,
        transport_security=streamable_http_transport_security(resource_settings),
        host=resource_settings.http_host,
    )

    with _serve(auth_app, auth_listener), _serve(resource_app, resource_listener):
        initialize_params = {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "clientInfo": {"name": "oauth-e2e", "version": "1"},
        }
        status, challenge_headers, _ = _json_rpc(
            resource_url,
            token=None,
            method="initialize",
            params=initialize_params,
        )
        assert status == 401
        challenge = challenge_headers["www-authenticate"]
        assert "resource_metadata=" in challenge
        metadata_url = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]

        status, _, metadata_body = _http(metadata_url)
        assert status == 200
        resource_metadata = json.loads(metadata_body)
        assert resource_metadata["resource"] == resource_url
        assert resource_metadata["authorization_servers"] == [issuer]

        status, _, auth_metadata_body = _http(f"{issuer}/.well-known/oauth-authorization-server")
        assert status == 200
        auth_metadata = json.loads(auth_metadata_body)
        assert auth_metadata["issuer"].rstrip("/") == issuer
        assert auth_metadata["code_challenge_methods_supported"] == ["S256"]

        authorize_query = urllib.parse.urlencode(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": CALLBACK,
                "response_type": "code",
                "code_challenge": CODE_CHALLENGE,
                "code_challenge_method": "S256",
                "state": "oauth-e2e-state",
                "scope": "serverfs:access",
                "resource": resource_url,
            }
        )
        status, authorize_headers, _ = _http(f"{issuer}/authorize?{authorize_query}")
        assert status == 302
        status, login_headers, _ = _http(authorize_headers["location"])
        assert status == 302
        callback = urllib.parse.urlparse(login_headers["location"])
        callback_params = urllib.parse.parse_qs(callback.query)
        assert callback_params["state"] == ["oauth-e2e-state"]
        assert callback_params["iss"][0].rstrip("/") == issuer
        authorization_code = callback_params["code"][0]

        status, _, token_body = _form(
            f"{issuer}/token",
            {
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": CALLBACK,
                "client_id": CLIENT_ID,
                "code_verifier": CODE_VERIFIER,
                "resource": resource_url,
            },
        )
        assert status == 200
        tokens = json.loads(token_body)
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        status, initialize_headers, initialize_body = _json_rpc(
            resource_url,
            token=access_token,
            method="initialize",
            params=initialize_params,
        )
        assert status == 200
        assert initialize_body is not None and "error" not in initialize_body
        # 2026-07-28 is a modern per-request protocol and is deliberately not
        # negotiated through the legacy initialize handshake. The SDK's newest
        # handshake-era revision is 2025-11-25.
        assert initialize_body["result"]["protocolVersion"] == "2025-11-25"
        session_id = initialize_headers["mcp-session-id"]

        status, _, notification_body = _json_rpc(
            resource_url,
            token=access_token,
            session_id=session_id,
            method="notifications/initialized",
            request_id=None,
        )
        assert status in {200, 202}
        assert notification_body is None

        status, _, tools_body = _json_rpc(
            resource_url,
            token=access_token,
            session_id=session_id,
            method="tools/list",
            request_id=2,
        )
        assert status == 200
        assert tools_body is not None and "error" not in tools_body
        tool_names = {item["name"] for item in tools_body["result"]["tools"]}
        assert {"list_workdirs", "read_text_file", "edit_text_file"}.issubset(tool_names)

        listed = _tool_call(resource_url, access_token, session_id, 3, "list_workdirs", {})
        assert listed["workdirs"][0]["access"] == "read-write"

        async def modern_roundtrip() -> tuple[str, set[str], dict[str, Any] | None]:
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {access_token}"},
                trust_env=False,
            ) as http_client:
                transport = streamable_http_client(
                    resource_url,
                    http_client=http_client,
                    terminate_on_close=False,
                )
                async with Client(
                    transport,
                    mode="2026-07-28",
                    raise_exceptions=True,
                ) as modern:
                    tools = await modern.list_tools()
                    result = await modern.call_tool("list_workdirs", {})
                    return (
                        str(modern.protocol_version),
                        {tool.name for tool in tools.tools},
                        result.structured_content,
                    )

        modern_protocol, modern_tools, modern_workdirs = asyncio.run(modern_roundtrip())
        assert modern_protocol == "2026-07-28"
        assert {"list_workdirs", "read_text_file", "edit_text_file"}.issubset(modern_tools)
        assert modern_workdirs is not None
        assert modern_workdirs["workdirs"][0]["access"] == "read-write"

        initial = _tool_call(
            resource_url,
            access_token,
            session_id,
            4,
            "read_text_file",
            {"workdir": "test", "path": "sample.txt"},
        )
        assert initial["content"] == "content\n"
        revision = initial["revision"]
        edited = _tool_call(
            resource_url,
            access_token,
            session_id,
            5,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "sample.txt",
                "expected_revision": revision,
                "edits": [{"old_text": "content", "new_text": "changed"}],
            },
        )
        assert edited["revision"] != revision
        assert sample.read_text(encoding="utf-8") == "changed\n"

        status, _, refresh_body = _form(
            f"{issuer}/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "resource": resource_url,
            },
        )
        assert status == 200
        refreshed = json.loads(refresh_body)
        new_access_token = refreshed["access_token"]
        new_refresh_token = refreshed["refresh_token"]
        assert new_access_token != access_token
        assert new_refresh_token != refresh_token

        old_status, _, _ = _json_rpc(
            resource_url,
            token=access_token,
            method="initialize",
            params=initialize_params,
        )
        assert old_status == 401

        wrong_resource_pair = auth_app.state.oauth_store.issue_token_pair(
            client_id=CLIENT_ID,
            scopes=["serverfs:access"],
            resource="http://127.0.0.1:9/wrong-resource",
            subject="github:123",
            issuer=issuer,
            access_ttl_seconds=300,
            refresh_ttl_seconds=300,
        )
        wrong_status, _, _ = _json_rpc(
            resource_url,
            token=wrong_resource_pair.access_token,
            method="initialize",
            params=initialize_params,
        )
        assert wrong_status == 401

        status, _, _ = _form(
            f"{issuer}/revoke",
            {
                "token": new_access_token,
                "token_type_hint": "access_token",
                "client_id": CLIENT_ID,
            },
        )
        assert status == 200
        revoked_status, _, _ = _json_rpc(
            resource_url,
            token=new_access_token,
            method="initialize",
            params=initialize_params,
        )
        assert revoked_status == 401
