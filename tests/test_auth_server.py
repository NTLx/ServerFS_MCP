"""End-to-end tests for the isolated ServerFS OAuth authorization core."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from urllib.parse import parse_qs, urlparse

from mcp.shared.auth import OAuthClientInformationFull
from starlette.testclient import TestClient

from serverfs_mcp.auth_clients import ClientMetadataResolver
from serverfs_mcp.auth_config import AuthServiceSettings
from serverfs_mcp.auth_server import create_auth_app
from serverfs_mcp.auth_store import OAuthStateStore

ISSUER = "http://127.0.0.1:8082"
RESOURCE = "http://127.0.0.1:8000/mcp"
REDIRECT = "http://127.0.0.1:9876/callback"
CLIENT_ID = "phase-c-client"
INTROSPECTION_SECRET = "phase-c-internal-secret"
CODE_VERIFIER = "phase-c-code-verifier-abcdefghijklmnopqrstuvwxyz0123456789"
CODE_CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode()).digest()).decode().rstrip("=")
)


def settings(tmp_path, **overrides) -> AuthServiceSettings:
    values = {
        "oauth_enabled": True,
        "public_url": ISSUER,
        "resource_url": RESOURCE,
        "state_dir": str(tmp_path),
        "allowed_principals": ("github:123",),
        "introspection_secret": INTROSPECTION_SECRET,
    }
    values.update(overrides)
    return AuthServiceSettings(**values)


def public_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=CLIENT_ID,
        redirect_uris=[REDIRECT],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        scope="serverfs:access",
    )


def register_client(app) -> None:
    asyncio.run(app.state.oauth_provider.register_client(public_client()))


def authorize(client: TestClient) -> tuple[str, str]:
    response = client.get(
        "/authorize",
        params={
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "state": "state-123",
            "scope": "serverfs:access",
            "resource": RESOURCE,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    login_url = response.headers["location"]
    assert login_url.startswith(f"{ISSUER}/login?transaction=")

    login_response = client.get(login_url, follow_redirects=False)
    assert login_response.status_code == 302
    callback = urlparse(login_response.headers["location"])
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == REDIRECT
    params = parse_qs(callback.query)
    assert params["state"] == ["state-123"]
    assert params["iss"] == [ISSUER]
    return params["code"][0], login_url


def exchange_code(client: TestClient, code: str, *, verifier: str = CODE_VERIFIER):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )


def introspect(client: TestClient, token: str, *, secret: str = INTROSPECTION_SECRET):
    return client.post(
        "/internal/introspect",
        headers={"Authorization": f"Bearer {secret}"},
        json={"token": token},
    )


class TestAuthorizationCore:
    def test_metadata_exposes_pkce_and_revocation_without_dcr(self, tmp_path) -> None:
        app = create_auth_app(settings(tmp_path), test_identity="github:123")
        register_client(app)
        with TestClient(app) as client:
            response = client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        payload = response.json()
        assert payload["issuer"] == ISSUER
        assert payload["authorization_endpoint"] == f"{ISSUER}/authorize"
        assert payload["token_endpoint"] == f"{ISSUER}/token"
        assert payload["revocation_endpoint"] == f"{ISSUER}/revoke"
        assert payload["code_challenge_methods_supported"] == ["S256"]
        assert payload["client_id_metadata_document_supported"] is True
        assert payload["authorization_response_iss_parameter_supported"] is True
        assert "registration_endpoint" not in payload

    def test_code_token_refresh_rotation_revocation_and_persistence(self, tmp_path) -> None:
        app = create_auth_app(settings(tmp_path), test_identity="github:123")
        register_client(app)
        with TestClient(app) as client:
            code, _ = authorize(client)
            token_response = exchange_code(client, code)
            assert token_response.status_code == 200, token_response.text
            tokens = token_response.json()
            access_token = tokens["access_token"]
            refresh_token = tokens["refresh_token"]
            assert tokens["scope"] == "serverfs:access"

            replay = exchange_code(client, code)
            assert replay.status_code == 400
            assert replay.json()["error"] == "invalid_grant"

            active = introspect(client, access_token)
            assert active.status_code == 200
            assert active.json()["active"] is True
            assert active.json()["resource"] == RESOURCE
            assert active.json()["iss"] == ISSUER
            assert active.json()["sub"] == "github:123"

            unauthorized = introspect(client, access_token, secret="wrong-secret")
            assert unauthorized.status_code == 401

            refreshed = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
            )
            assert refreshed.status_code == 200, refreshed.text
            rotated = refreshed.json()
            assert rotated["access_token"] != access_token
            assert rotated["refresh_token"] != refresh_token

            assert introspect(client, access_token).json() == {"active": False}
            refresh_replay = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
            )
            assert refresh_replay.status_code == 400
            assert refresh_replay.json()["error"] == "invalid_grant"

            revoke = client.post(
                "/revoke",
                data={
                    "token": rotated["access_token"],
                    "token_type_hint": "access_token",
                    "client_id": CLIENT_ID,
                },
            )
            assert revoke.status_code == 200
            assert introspect(client, rotated["access_token"]).json() == {"active": False}
            paired_refresh = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": rotated["refresh_token"],
                    "client_id": CLIENT_ID,
                },
            )
            assert paired_refresh.status_code == 400

        restarted = OAuthStateStore(str(tmp_path))
        restarted.initialize()
        assert restarted.get_client(CLIENT_ID) is not None
        assert restarted.introspect_access_token(access_token) == {"active": False}

        db_bytes = (tmp_path / "auth.sqlite3").read_bytes()
        assert access_token.encode() not in db_bytes
        assert refresh_token.encode() not in db_bytes
        if os.name != "nt":
            assert (tmp_path / "auth.sqlite3").stat().st_mode & 0o777 == 0o600

    def test_wrong_pkce_does_not_consume_code(self, tmp_path) -> None:
        app = create_auth_app(settings(tmp_path), test_identity="github:123")
        register_client(app)
        with TestClient(app) as client:
            code, _ = authorize(client)
            wrong = exchange_code(client, code, verifier="wrong-verifier")
            assert wrong.status_code == 400
            assert wrong.json()["error"] == "invalid_grant"
            correct = exchange_code(client, code)
            assert correct.status_code == 200

    def test_login_transaction_is_single_use(self, tmp_path) -> None:
        app = create_auth_app(settings(tmp_path), test_identity="github:123")
        register_client(app)
        with TestClient(app) as client:
            _, login_url = authorize(client)
            replay = client.get(login_url, follow_redirects=False)
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_or_expired_transaction"

    def test_authorize_rejects_wrong_resource(self, tmp_path) -> None:
        app = create_auth_app(settings(tmp_path), test_identity="github:123")
        register_client(app)
        with TestClient(app) as client:
            response = client.get(
                "/authorize",
                params={
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT,
                    "response_type": "code",
                    "code_challenge": CODE_CHALLENGE,
                    "code_challenge_method": "S256",
                    "scope": "serverfs:access",
                    "resource": "https://wrong.example.com/mcp",
                },
                follow_redirects=False,
            )
        assert response.status_code == 302
        params = parse_qs(urlparse(response.headers["location"]).query)
        assert params["error"] == ["invalid_target"]

    def test_authenticated_but_unlisted_identity_is_denied_before_code_issuance(
        self, tmp_path
    ) -> None:
        app = create_auth_app(
            settings(tmp_path, allowed_principals=()),
            test_identity="github:123",
        )
        register_client(app)
        with TestClient(app) as client:
            response = client.get(
                "/authorize",
                params={
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT,
                    "response_type": "code",
                    "code_challenge": CODE_CHALLENGE,
                    "code_challenge_method": "S256",
                    "scope": "serverfs:access",
                    "resource": RESOURCE,
                },
                follow_redirects=False,
            )
            denied = client.get(response.headers["location"], follow_redirects=False)
        assert denied.status_code == 403
        assert denied.json()["error"] == "access_denied"
        with TestClient(app) as client:
            denied_again = client.get(response.headers["location"], follow_redirects=False)
        assert denied_again.status_code == 403
        assert denied_again.json()["error"] == "access_denied"

    def test_without_test_identity_login_fails_closed(self, tmp_path) -> None:
        app = create_auth_app(settings(tmp_path))
        register_client(app)
        with TestClient(app) as client:
            response = client.get(
                "/authorize",
                params={
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT,
                    "response_type": "code",
                    "code_challenge": CODE_CHALLENGE,
                    "code_challenge_method": "S256",
                    "scope": "serverfs:access",
                    "resource": RESOURCE,
                },
                follow_redirects=False,
            )
            login_response = client.get(response.headers["location"], follow_redirects=False)
        assert login_response.status_code == 503
        assert login_response.json()["error"] == "identity_provider_unavailable"

    def test_introspection_body_is_bounded(self, tmp_path) -> None:
        app = create_auth_app(settings(tmp_path), test_identity="github:123")
        with TestClient(app) as client:
            response = client.post(
                "/internal/introspect",
                headers={"Authorization": f"Bearer {INTROSPECTION_SECRET}"},
                content=b"x" * 5000,
            )
        assert response.status_code == 413


class StaticCimdFetcher:
    def __init__(self, document: dict[str, object]) -> None:
        self.document = document
        self.calls: list[str] = []

    def fetch(self, client_id: str):
        self.calls.append(client_id)
        return self.document


class TestClientRegistration:
    def test_dcr_is_advertised_only_when_enabled_and_public_client_registers(
        self, tmp_path
    ) -> None:
        app = create_auth_app(settings(tmp_path, dcr_enabled=True), test_identity="github:123")
        with TestClient(app) as client:
            metadata = client.get("/.well-known/oauth-authorization-server").json()
            assert metadata["registration_endpoint"] == f"{ISSUER}/register"
            response = client.post(
                "/register",
                json={
                    "redirect_uris": [REDIRECT],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": "serverfs:access",
                    "application_type": "native",
                },
            )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["client_id"]
        assert payload["token_endpoint_auth_method"] == "none"
        assert payload.get("client_secret") is None

    def test_dcr_rejects_confidential_client(self, tmp_path) -> None:
        app = create_auth_app(settings(tmp_path, dcr_enabled=True), test_identity="github:123")
        with TestClient(app) as client:
            response = client.post(
                "/register",
                json={
                    "redirect_uris": [REDIRECT],
                    "token_endpoint_auth_method": "client_secret_post",
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "scope": "serverfs:access",
                },
            )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_client_metadata"

    def test_cimd_client_is_resolved_without_registration(self, tmp_path) -> None:
        client_id = "https://client.example.com/oauth/client.json"
        document = {
            "client_id": client_id,
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "serverfs:access",
        }
        fetcher = StaticCimdFetcher(document)
        resolver = ClientMetadataResolver(fetcher=fetcher)
        app = create_auth_app(
            settings(tmp_path),
            client_resolver=resolver,
            test_identity="github:123",
        )
        with TestClient(app) as client:
            response = client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": REDIRECT,
                    "response_type": "code",
                    "code_challenge": CODE_CHALLENGE,
                    "code_challenge_method": "S256",
                    "scope": "serverfs:access",
                    "resource": RESOURCE,
                },
                follow_redirects=False,
            )
        assert response.status_code == 302
        assert response.headers["location"].startswith(f"{ISSUER}/login?transaction=")
        assert fetcher.calls == [client_id]

    def test_pre_registered_clients_file_is_loaded(self, tmp_path) -> None:
        clients_file = tmp_path / "clients.json"
        clients_file.write_text(
            json.dumps(
                [
                    {
                        "client_id": "operator-client",
                        "redirect_uris": [REDIRECT],
                        "token_endpoint_auth_method": "none",
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "scope": "serverfs:access",
                    }
                ]
            ),
            encoding="utf-8",
        )
        app = create_auth_app(
            settings(tmp_path / "state", clients_file=str(clients_file)),
            test_identity="github:123",
        )
        loaded = asyncio.run(app.state.oauth_provider.get_client("operator-client"))
        assert loaded is not None
        assert loaded.client_id == "operator-client"
