"""Focused concurrency and corruption gates for the OAuth state store."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from serverfs_mcp.auth_store import OAuthStateStore

CLIENT_ID = "security-client"
RESOURCE = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com"
REDIRECT = "http://127.0.0.1:9876/callback"


def public_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=CLIENT_ID,
        redirect_uris=[REDIRECT],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        scope="serverfs:access",
    )


def prepared_authorization_code(store: OAuthStateStore) -> str:
    client = public_client()
    store.save_client(client)
    transaction = store.create_pending_authorization(
        client,
        AuthorizationParams(
            state="state",
            scopes=["serverfs:access"],
            code_challenge="challenge",
            redirect_uri=REDIRECT,
            redirect_uri_provided_explicitly=True,
            resource=RESOURCE,
        ),
        scopes=["serverfs:access"],
        resource=RESOURCE,
    )
    completed = store.complete_pending_authorization(transaction, subject="github:123")
    assert completed is not None
    return completed.code.code


def test_concurrent_authorization_code_consumption_has_exactly_one_winner(tmp_path) -> None:
    store = OAuthStateStore(str(tmp_path))
    store.initialize()
    code = prepared_authorization_code(store)
    barrier = threading.Barrier(2)

    def consume():
        barrier.wait()
        return store.consume_authorization_code(CLIENT_ID, code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(consume), executor.submit(consume)]
        results = [future.result() for future in futures]

    assert sum(result is not None for result in results) == 1
    assert store.load_authorization_code(CLIENT_ID, code) is None


def test_concurrent_refresh_rotation_has_exactly_one_winner(tmp_path) -> None:
    store = OAuthStateStore(str(tmp_path))
    store.initialize()
    original = store.issue_token_pair(
        client_id=CLIENT_ID,
        scopes=["serverfs:access"],
        resource=RESOURCE,
        subject="github:123",
        issuer=ISSUER,
        access_ttl_seconds=300,
        refresh_ttl_seconds=300,
    )
    barrier = threading.Barrier(2)

    def rotate():
        barrier.wait()
        return store.rotate_refresh_token(
            client_id=CLIENT_ID,
            token_value=original.refresh_token,
            scopes=["serverfs:access"],
            issuer=ISSUER,
            access_ttl_seconds=300,
            refresh_ttl_seconds=300,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [executor.submit(rotate), executor.submit(rotate)]]

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert store.introspect_access_token(original.access_token) == {"active": False}
    assert store.load_refresh_token(CLIENT_ID, original.refresh_token) is None
    assert store.introspect_access_token(winners[0].access_token)["active"] is True


def test_unknown_auth_database_schema_fails_closed_on_startup(tmp_path) -> None:
    store = OAuthStateStore(str(tmp_path))
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE schema_meta SET version = 999")

    restarted = OAuthStateStore(str(tmp_path))
    with pytest.raises(RuntimeError, match="unsupported OAuth state schema version 999"):
        restarted.initialize()
