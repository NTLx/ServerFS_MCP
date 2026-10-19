"""Client registration and CIMD security tests for serverfs-auth."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from serverfs_mcp.auth_clients import (
    CimdError,
    CimdFetcher,
    ClientMetadataResolver,
    client_from_cimd,
    load_pre_registered_clients,
    validate_cimd_addresses,
    validate_client_identifier_url,
)

CLIENT_ID = "https://client.example.com/oauth/client.json"
REDIRECT_URI = "http://127.0.0.1:9876/callback"


def valid_document(client_id: str = CLIENT_ID) -> dict[str, object]:
    return {
        "client_id": client_id,
        "client_name": "ServerFS test client",
        "redirect_uris": [REDIRECT_URI],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "serverfs:access",
    }


class StubFetcher:
    def __init__(self, documents: dict[str, dict[str, object] | Exception]) -> None:
        self.documents = documents
        self.calls: list[str] = []

    def fetch(self, client_id: str):
        self.calls.append(client_id)
        value = self.documents[client_id]
        if isinstance(value, Exception):
            raise value
        return value


class TestClientIdentifierValidation:
    @pytest.mark.parametrize(
        "value",
        [
            "http://client.example.com/oauth/client.json",
            "https://client.example.com/",
            "https://user@client.example.com/oauth/client.json",
            "https://client.example.com/oauth/client.json?version=1",
            "https://client.example.com/oauth/client.json#fragment",
            "https://client.example.com/oauth/../client.json",
            "https://client.example.com/oauth/%2E%2E/client.json",
        ],
    )
    def test_unsafe_or_unstable_client_identifier_is_rejected(self, value: str) -> None:
        with pytest.raises(CimdError):
            validate_client_identifier_url(value)

    def test_valid_identifier_preserves_exact_authority_and_path(self) -> None:
        assert validate_client_identifier_url(CLIENT_ID) == (
            "client.example.com",
            443,
            "/oauth/client.json",
        )

    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "192.0.2.1"],
    )
    def test_special_use_addresses_are_rejected(self, address: str) -> None:
        with pytest.raises(CimdError):
            validate_cimd_addresses([address])

    def test_all_resolved_addresses_must_be_global(self) -> None:
        with pytest.raises(CimdError):
            validate_cimd_addresses(["8.8.8.8", "127.0.0.1"])
        assert validate_cimd_addresses(["8.8.8.8", "1.1.1.1"]) == (
            "8.8.8.8",
            "1.1.1.1",
        )

    def test_fetcher_rejects_private_dns_answer_before_connecting(self) -> None:
        def private_resolver(_host: str, _port: int, *, type: int):
            assert type == socket.SOCK_STREAM
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

        fetcher = CimdFetcher(resolver=private_resolver)
        with pytest.raises(CimdError, match="non-global"):
            fetcher.fetch(CLIENT_ID)


class TestCimdDocumentValidation:
    def test_valid_public_pkce_client(self) -> None:
        client = client_from_cimd(CLIENT_ID, valid_document())
        assert client.client_id == CLIENT_ID
        assert client.token_endpoint_auth_method == "none"
        assert [str(uri) for uri in client.redirect_uris or []] == [REDIRECT_URI]
        assert client.scope == "serverfs:access"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("client_id", "https://other.example.com/oauth/client.json"),
            ("token_endpoint_auth_method", "client_secret_post"),
            ("scope", "serverfs:admin"),
            ("redirect_uris", []),
            ("grant_types", ["refresh_token"]),
            ("response_types", ["token"]),
        ],
    )
    def test_invalid_document_is_rejected(self, field: str, value: object) -> None:
        document = valid_document()
        document[field] = value
        with pytest.raises(CimdError):
            client_from_cimd(CLIENT_ID, document)

    @pytest.mark.parametrize(
        "field", ["client_secret", "client_secret_expires_at", "jwks", "jwks_uri"]
    )
    def test_shared_secret_or_key_client_metadata_is_rejected(self, field: str) -> None:
        document = valid_document()
        document[field] = "forbidden"
        with pytest.raises(CimdError):
            client_from_cimd(CLIENT_ID, document)


class TestCimdCache:
    def test_valid_document_is_cached_and_cache_is_bounded(self) -> None:
        client_two = "https://two.example.com/oauth/client.json"
        client_three = "https://three.example.com/oauth/client.json"
        fetcher = StubFetcher(
            {
                CLIENT_ID: valid_document(),
                client_two: valid_document(client_two),
                client_three: valid_document(client_three),
            }
        )
        resolver = ClientMetadataResolver(fetcher=fetcher, cache_seconds=60, max_entries=2)

        assert resolver.resolve(CLIENT_ID) is not None
        assert resolver.resolve(CLIENT_ID) is not None
        assert fetcher.calls == [CLIENT_ID]
        assert resolver.resolve(client_two) is not None
        assert resolver.resolve(client_three) is not None
        assert len(resolver._cache) == 2
        assert CLIENT_ID not in resolver._cache

    def test_fetch_errors_and_invalid_documents_are_not_cached(self) -> None:
        fetcher = StubFetcher({CLIENT_ID: CimdError("offline")})
        resolver = ClientMetadataResolver(fetcher=fetcher)
        assert resolver.resolve(CLIENT_ID) is None
        assert resolver.resolve(CLIENT_ID) is None
        assert fetcher.calls == [CLIENT_ID, CLIENT_ID]


class TestPreRegisteredClients:
    def test_public_clients_file_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "clients.json"
        path.write_text(json.dumps([valid_document("static-client")]), encoding="utf-8")
        clients = load_pre_registered_clients(str(path))
        assert len(clients) == 1
        assert clients[0].client_id == "static-client"
        assert clients[0].token_endpoint_auth_method == "none"

    def test_duplicate_or_confidential_pre_registration_fails(self, tmp_path: Path) -> None:
        duplicate = tmp_path / "duplicate.json"
        duplicate.write_text(
            json.dumps([valid_document("same"), valid_document("same")]),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="duplicate"):
            load_pre_registered_clients(str(duplicate))

        confidential = valid_document("secret-client")
        confidential["token_endpoint_auth_method"] = "client_secret_post"
        confidential["client_secret"] = "secret"
        path = tmp_path / "confidential.json"
        path.write_text(json.dumps([confidential]), encoding="utf-8")
        with pytest.raises(ValueError, match="public PKCE"):
            load_pre_registered_clients(str(path))
