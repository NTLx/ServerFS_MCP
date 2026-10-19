"""OAuth Resource Server boundary tests for optional remote MCP authentication."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
from collections.abc import Mapping
from typing import Any

import pytest
from mcp.server.auth.provider import AccessToken

import serverfs_mcp.oauth_resource as oauth_module
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server, streamable_http_transport_security
from serverfs_mcp.oauth_resource import OAuthIntrospectionVerifier
from serverfs_mcp.workdirs import WorkdirRegistry

RESOURCE = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com"


def oauth_settings() -> Settings:
    return Settings(
        oauth_enabled=True,
        oauth_resource_url=RESOURCE,
        oauth_issuer_url=ISSUER,
        oauth_required_scopes=("serverfs:access",),
        oauth_introspection_secret="shared-secret",
    )


class StaticVerifier:
    def __init__(self, access_token: AccessToken | None) -> None:
        self.access_token = access_token
        self.calls: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        self.calls.append(token)
        return self.access_token


def access_token(
    *,
    scopes: list[str] | None = None,
    resource: str = RESOURCE,
    expires_at: int | None = None,
) -> AccessToken:
    return AccessToken(
        token="",
        client_id="test-client",
        scopes=scopes or ["serverfs:access"],
        expires_at=expires_at or int(time.time()) + 300,
        resource=resource,
        subject="github:123",
        claims={"iss": ISSUER},
    )


async def _asgi_request(
    server,
    *,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    settings = oauth_settings()
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=streamable_http_transport_security(settings),
        host=settings.http_host,
    )
    messages: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("oauth-test", 1),
        "server": ("serverfs-mcp", 8000),
    }
    async with app.router.lifespan_context(app):
        await app(scope, receive, send)

    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_headers = {
        key.decode("latin1").lower(): value.decode("latin1")
        for key, value in response_start.get("headers", [])
    }
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return int(response_start["status"]), response_headers, response_body


def initialize_body() -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "oauth-test", "version": "1"},
            },
        }
    ).encode()


def mcp_headers(token: str | None = None) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"host", b"serverfs-mcp:8000"),
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return headers


class TestResourceServerBoundary:
    def test_oauth_disabled_server_has_no_auth_boundary(self) -> None:
        server = create_server(Settings(), WorkdirRegistry([]))
        assert server.settings.auth is None
        assert server._token_verifier is None

    def test_oauth_enabled_server_uses_exact_resource_validation(self) -> None:
        verifier = StaticVerifier(access_token())
        server = create_server(oauth_settings(), WorkdirRegistry([]), token_verifier=verifier)
        assert server.settings.auth is not None
        assert str(server.settings.auth.issuer_url) == ISSUER
        assert str(server.settings.auth.resource_server_url) == RESOURCE
        assert server.settings.auth.required_scopes == ["serverfs:access"]
        assert server.settings.auth.validate_token_resource is True
        assert server._token_verifier is verifier

    def test_token_verifier_is_rejected_when_oauth_is_disabled(self) -> None:
        with pytest.raises(ValueError, match="OAuth is disabled"):
            create_server(Settings(), WorkdirRegistry([]), token_verifier=StaticVerifier(None))

    def test_unauthenticated_mcp_request_gets_rfc9728_challenge(self) -> None:
        server = create_server(
            oauth_settings(), WorkdirRegistry([]), token_verifier=StaticVerifier(access_token())
        )
        status, headers, _ = asyncio.run(
            _asgi_request(
                server,
                method="POST",
                path="/mcp",
                headers=mcp_headers(),
                body=initialize_body(),
            )
        )
        assert status == 401
        assert headers["www-authenticate"].startswith("Bearer ")
        assert (
            'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"'
            in headers["www-authenticate"]
        )

    def test_protected_resource_metadata_is_public_and_correct(self) -> None:
        server = create_server(
            oauth_settings(), WorkdirRegistry([]), token_verifier=StaticVerifier(access_token())
        )
        status, _, body = asyncio.run(
            _asgi_request(
                server,
                method="GET",
                path="/.well-known/oauth-protected-resource/mcp",
                headers=[(b"host", b"serverfs-mcp:8000")],
            )
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["resource"] == RESOURCE
        assert payload["authorization_servers"] == [ISSUER]
        assert payload["scopes_supported"] == ["serverfs:access"]

    def test_valid_token_reaches_mcp_transport(self) -> None:
        verifier = StaticVerifier(access_token())
        server = create_server(oauth_settings(), WorkdirRegistry([]), token_verifier=verifier)
        status, _, _ = asyncio.run(
            _asgi_request(
                server,
                method="POST",
                path="/mcp",
                headers=mcp_headers("opaque-good"),
                body=initialize_body(),
            )
        )
        assert status == 200
        assert verifier.calls == ["opaque-good"]

    def test_wrong_resource_token_is_rejected(self) -> None:
        server = create_server(
            oauth_settings(),
            WorkdirRegistry([]),
            token_verifier=StaticVerifier(access_token(resource="https://other.example.com/mcp")),
        )
        status, _, _ = asyncio.run(
            _asgi_request(
                server,
                method="POST",
                path="/mcp",
                headers=mcp_headers("wrong-resource"),
                body=initialize_body(),
            )
        )
        assert status == 401

    def test_wrong_scope_token_is_rejected(self) -> None:
        server = create_server(
            oauth_settings(),
            WorkdirRegistry([]),
            token_verifier=StaticVerifier(access_token(scopes=["other:scope"])),
        )
        status, _, _ = asyncio.run(
            _asgi_request(
                server,
                method="POST",
                path="/mcp",
                headers=mcp_headers("wrong-scope"),
                body=initialize_body(),
            )
        )
        assert status == 403

    def test_expired_or_revoked_token_is_rejected(self) -> None:
        expired = create_server(
            oauth_settings(),
            WorkdirRegistry([]),
            token_verifier=StaticVerifier(access_token(expires_at=int(time.time()) - 1)),
        )
        revoked = create_server(
            oauth_settings(), WorkdirRegistry([]), token_verifier=StaticVerifier(None)
        )
        for server, token in ((expired, "expired"), (revoked, "revoked")):
            status, _, _ = asyncio.run(
                _asgi_request(
                    server,
                    method="POST",
                    path="/mcp",
                    headers=mcp_headers(token),
                    body=initialize_body(),
                )
            )
            assert status == 401


class StubIntrospectionVerifier(OAuthIntrospectionVerifier):
    def __init__(self, payload: Mapping[str, Any] | None) -> None:
        super().__init__(
            introspection_url="http://serverfs-auth:8082/internal/introspect",
            introspection_secret="secret",
            timeout_seconds=1,
            expected_resource=RESOURCE,
            expected_issuer=ISSUER,
        )
        self.payload = payload

    def _fetch_introspection(self, token: str) -> Mapping[str, Any] | None:
        return self.payload


class TestIntrospectionVerifier:
    def valid_payload(self) -> dict[str, Any]:
        return {
            "active": True,
            "client_id": "client-1",
            "scope": "serverfs:access",
            "sub": "github:123",
            "exp": int(time.time()) + 300,
            "resource": RESOURCE,
            "iss": ISSUER,
        }

    def test_active_payload_maps_to_mcp_access_token_without_retaining_bearer(self) -> None:
        result = asyncio.run(
            StubIntrospectionVerifier(self.valid_payload()).verify_token("secret-token")
        )
        assert result is not None
        assert result.token == ""
        assert result.client_id == "client-1"
        assert result.scopes == ["serverfs:access"]
        assert result.subject == "github:123"
        assert result.resource == RESOURCE
        assert result.claims == {"iss": ISSUER}

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("active", False),
            ("client_id", ""),
            ("scope", ""),
            ("scope", "serverfs:access serverfs:access"),
            ("sub", ""),
            ("resource", "https://other.example.com/mcp"),
            ("iss", "https://other.example.com"),
            ("exp", 0),
        ],
    )
    def test_invalid_or_inactive_payload_fails_closed(self, field: str, value: Any) -> None:
        payload = self.valid_payload()
        payload[field] = value
        assert asyncio.run(StubIntrospectionVerifier(payload).verify_token("opaque")) is None

    def test_missing_or_non_object_payload_fails_closed(self) -> None:
        assert asyncio.run(StubIntrospectionVerifier(None).verify_token("opaque")) is None


class FakeResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class CapturingOpener:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.request = None
        self.timeout = None

    def open(self, request, *, timeout: float):
        self.request = request
        self.timeout = timeout
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TestIntrospectionTransport:
    def make_verifier(self) -> OAuthIntrospectionVerifier:
        return OAuthIntrospectionVerifier(
            introspection_url="http://serverfs-auth:8082/internal/introspect",
            introspection_secret="private-shared-secret",
            timeout_seconds=1.25,
            expected_resource=RESOURCE,
            expected_issuer=ISSUER,
        )

    def test_private_introspection_request_is_bounded_post_json(self) -> None:
        opener = CapturingOpener(FakeResponse(b'{"active":false}'))
        verifier = self.make_verifier()
        verifier._opener = opener

        assert verifier._fetch_introspection("opaque-user-token") == {"active": False}
        assert opener.request is not None
        assert opener.request.get_method() == "POST"
        assert opener.request.full_url == "http://serverfs-auth:8082/internal/introspect"
        assert opener.request.get_header("Authorization") == "Bearer private-shared-secret"
        assert opener.request.get_header("Content-type") == "application/json"
        assert json.loads(opener.request.data) == {"token": "opaque-user-token"}
        assert opener.timeout == 1.25

    def test_transport_failure_is_fail_closed_and_logs_no_secret(self, monkeypatch) -> None:
        logged: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        monkeypatch.setattr(
            oauth_module.jsonlog,
            "warning",
            lambda *args, **kwargs: logged.append((args, kwargs)),
        )
        opener = CapturingOpener(urllib.error.URLError("offline"))
        verifier = self.make_verifier()
        verifier._opener = opener

        assert verifier._fetch_introspection("opaque-user-token") is None
        rendered = repr(logged)
        assert "private-shared-secret" not in rendered
        assert "opaque-user-token" not in rendered
        assert "transport_error" in rendered
