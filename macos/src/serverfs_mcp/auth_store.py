"""SQLite persistence for the optional ServerFS OAuth authorization service."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull

_SCHEMA_VERSION = 1
_AUTHORIZATION_CODE_TTL_SECONDS = 300
_LOGIN_TRANSACTION_TTL_SECONDS = 300
_UPSTREAM_LOGIN_TTL_SECONDS = 600


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scopes_json(scopes: list[str]) -> str:
    return json.dumps(scopes, separators=(",", ":"))


def _load_scopes(raw: str) -> list[str]:
    value = json.loads(raw)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("invalid scopes payload in OAuth state database")
    return value


@dataclass(frozen=True)
class CompletedAuthorization:
    code: AuthorizationCode
    state: str | None


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: int


@dataclass(frozen=True)
class UpstreamLoginTransaction:
    provider: str
    pending_authorization_hash: str
    code_verifier: str


class OAuthStateStore:
    """Small single-instance OAuth state store with hashed bearer material."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "auth.sqlite3"

    def initialize(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_authorizations (
                    transaction_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    state TEXT,
                    scopes_json TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_uri_provided INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS upstream_login_transactions (
                    state_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    pending_authorization_hash TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authorization_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    code_challenge TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_uri_provided INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS access_tokens (
                    token_hash TEXT PRIMARY KEY,
                    pair_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    issuer TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS access_tokens_pair_idx ON access_tokens(pair_id);
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    pair_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS refresh_tokens_pair_idx ON refresh_tokens(pair_id);
                """
            )
            row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (_SCHEMA_VERSION,))
            elif int(row["version"]) != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported OAuth state schema version {row['version']}; "
                    f"expected {_SCHEMA_VERSION}"
                )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def save_client(self, client: OAuthClientInformationFull) -> None:
        payload = client.model_dump_json(exclude_none=False)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_clients(client_id, payload_json) VALUES (?, ?)",
                (client.client_id, payload),
            )

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row["payload_json"])

    def create_pending_authorization(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
        *,
        scopes: list[str],
        resource: str,
    ) -> str:
        transaction = secrets.token_urlsafe(32)
        expires_at = time.time() + _LOGIN_TRANSACTION_TTL_SECONDS
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_authorizations(
                    transaction_hash, client_id, state, scopes_json, code_challenge,
                    redirect_uri, redirect_uri_provided, resource, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _digest(transaction),
                    client.client_id,
                    params.state,
                    _scopes_json(scopes),
                    params.code_challenge,
                    str(params.redirect_uri),
                    1 if params.redirect_uri_provided_explicitly else 0,
                    resource,
                    expires_at,
                ),
            )
        return transaction

    def complete_pending_authorization(
        self, transaction: str, *, subject: str
    ) -> CompletedAuthorization | None:
        return self.complete_pending_authorization_hash(_digest(transaction), subject=subject)

    def complete_pending_authorization_hash(
        self, transaction_hash: str, *, subject: str
    ) -> CompletedAuthorization | None:
        now = time.time()
        code_value = secrets.token_urlsafe(32)
        code_expires_at = now + _AUTHORIZATION_CODE_TTL_SECONDS
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM pending_authorizations WHERE transaction_hash = ?",
                (transaction_hash,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM pending_authorizations WHERE transaction_hash = ?",
                (transaction_hash,),
            )
            if float(row["expires_at"]) <= now:
                return None
            conn.execute(
                """
                INSERT INTO authorization_codes(
                    code_hash, client_id, scopes_json, expires_at, code_challenge,
                    redirect_uri, redirect_uri_provided, resource, subject
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _digest(code_value),
                    row["client_id"],
                    row["scopes_json"],
                    code_expires_at,
                    row["code_challenge"],
                    row["redirect_uri"],
                    row["redirect_uri_provided"],
                    row["resource"],
                    subject,
                ),
            )
            code = AuthorizationCode(
                code=code_value,
                scopes=_load_scopes(row["scopes_json"]),
                expires_at=code_expires_at,
                client_id=row["client_id"],
                code_challenge=row["code_challenge"],
                redirect_uri=row["redirect_uri"],
                redirect_uri_provided_explicitly=bool(row["redirect_uri_provided"]),
                resource=row["resource"],
                subject=subject,
            )
            return CompletedAuthorization(code=code, state=row["state"])

    def create_upstream_login(
        self,
        transaction: str,
        *,
        provider: str,
        code_verifier: str,
    ) -> str | None:
        pending_hash = _digest(transaction)
        state = secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT expires_at FROM pending_authorizations WHERE transaction_hash = ?",
                (pending_hash,),
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                return None
            conn.execute(
                """
                INSERT INTO upstream_login_transactions(
                    state_hash, provider, pending_authorization_hash, code_verifier, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _digest(state),
                    provider,
                    pending_hash,
                    code_verifier,
                    now + _UPSTREAM_LOGIN_TTL_SECONDS,
                ),
            )
        return state

    def consume_upstream_login(
        self, state: str, *, provider: str
    ) -> UpstreamLoginTransaction | None:
        now = time.time()
        state_hash = _digest(state)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM upstream_login_transactions WHERE state_hash = ? AND provider = ?",
                (state_hash, provider),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM upstream_login_transactions WHERE state_hash = ?", (state_hash,)
            )
            if float(row["expires_at"]) <= now:
                return None
            return UpstreamLoginTransaction(
                provider=row["provider"],
                pending_authorization_hash=row["pending_authorization_hash"],
                code_verifier=row["code_verifier"],
            )

    def discard_pending_authorization_hash(self, transaction_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM pending_authorizations WHERE transaction_hash = ?", (transaction_hash,)
            )

    def load_authorization_code(self, client_id: str, code_value: str) -> AuthorizationCode | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM authorization_codes WHERE code_hash = ? AND client_id = ?",
                (_digest(code_value), client_id),
            ).fetchone()
        if row is None:
            return None
        return self._authorization_code_from_row(row, code_value)

    def consume_authorization_code(
        self, client_id: str, code_value: str
    ) -> AuthorizationCode | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM authorization_codes WHERE code_hash = ? AND client_id = ?",
                (_digest(code_value), client_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM authorization_codes WHERE code_hash = ?",
                (_digest(code_value),),
            )
            return self._authorization_code_from_row(row, code_value)

    @staticmethod
    def _authorization_code_from_row(row: sqlite3.Row, code_value: str) -> AuthorizationCode:
        return AuthorizationCode(
            code=code_value,
            scopes=_load_scopes(row["scopes_json"]),
            expires_at=float(row["expires_at"]),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_provided"]),
            resource=row["resource"],
            subject=row["subject"],
        )

    def issue_token_pair(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str,
        subject: str,
        issuer: str,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> TokenPair:
        with self._connect() as conn:
            return self._insert_token_pair(
                conn,
                client_id=client_id,
                scopes=scopes,
                resource=resource,
                subject=subject,
                issuer=issuer,
                access_ttl_seconds=access_ttl_seconds,
                refresh_ttl_seconds=refresh_ttl_seconds,
            )

    def _insert_token_pair(
        self,
        conn: sqlite3.Connection,
        *,
        client_id: str,
        scopes: list[str],
        resource: str,
        subject: str,
        issuer: str,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> TokenPair:
        now = int(time.time())
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(48)
        pair_id = secrets.token_hex(16)
        access_expires_at = now + access_ttl_seconds
        refresh_expires_at = now + refresh_ttl_seconds
        scopes_json = _scopes_json(scopes)
        conn.execute(
            """
            INSERT INTO access_tokens(
                token_hash, pair_id, client_id, scopes_json, expires_at, resource, subject, issuer
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _digest(access_token),
                pair_id,
                client_id,
                scopes_json,
                access_expires_at,
                resource,
                subject,
                issuer,
            ),
        )
        conn.execute(
            """
            INSERT INTO refresh_tokens(
                token_hash, pair_id, client_id, scopes_json, expires_at, resource, subject
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _digest(refresh_token),
                pair_id,
                client_id,
                scopes_json,
                refresh_expires_at,
                resource,
                subject,
            ),
        )
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
        )

    def load_refresh_token(self, client_id: str, token_value: str) -> RefreshToken | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash = ? AND client_id = ?",
                (_digest(token_value), client_id),
            ).fetchone()
        if row is None:
            return None
        return RefreshToken(
            token=token_value,
            client_id=row["client_id"],
            scopes=_load_scopes(row["scopes_json"]),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["subject"],
        )

    def rotate_refresh_token(
        self,
        *,
        client_id: str,
        token_value: str,
        scopes: list[str],
        issuer: str,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> TokenPair | None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash = ? AND client_id = ?",
                (_digest(token_value), client_id),
            ).fetchone()
            if row is None or int(row["expires_at"]) <= now:
                return None
            original_scopes = _load_scopes(row["scopes_json"])
            if any(scope not in original_scopes for scope in scopes):
                return None
            pair_id = row["pair_id"]
            conn.execute("DELETE FROM access_tokens WHERE pair_id = ?", (pair_id,))
            conn.execute("DELETE FROM refresh_tokens WHERE pair_id = ?", (pair_id,))
            return self._insert_token_pair(
                conn,
                client_id=client_id,
                scopes=scopes,
                resource=row["resource"],
                subject=row["subject"],
                issuer=issuer,
                access_ttl_seconds=access_ttl_seconds,
                refresh_ttl_seconds=refresh_ttl_seconds,
            )

    def load_access_token(self, token_value: str) -> AccessToken | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM access_tokens WHERE token_hash = ?", (_digest(token_value),)
            ).fetchone()
        if row is None:
            return None
        return AccessToken(
            token=token_value,
            client_id=row["client_id"],
            scopes=_load_scopes(row["scopes_json"]),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["subject"],
            claims={"iss": row["issuer"]},
        )

    def revoke_token_pair(self, token_value: str) -> None:
        token_hash = _digest(token_value)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT pair_id FROM access_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT pair_id FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
                ).fetchone()
            if row is None:
                return
            pair_id = row["pair_id"]
            conn.execute("DELETE FROM access_tokens WHERE pair_id = ?", (pair_id,))
            conn.execute("DELETE FROM refresh_tokens WHERE pair_id = ?", (pair_id,))

    def introspect_access_token(self, token_value: str) -> dict[str, object]:
        token = self.load_access_token(token_value)
        if token is None or token.expires_at is None or token.expires_at <= int(time.time()):
            return {"active": False}
        return {
            "active": True,
            "client_id": token.client_id,
            "scope": " ".join(token.scopes),
            "sub": token.subject,
            "exp": token.expires_at,
            "resource": token.resource,
            "iss": (token.claims or {}).get("iss"),
        }
