"""OAuth Authorization Server provider backed by the ServerFS SQLite store."""

from __future__ import annotations

from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .auth_store import CompletedAuthorization, OAuthStateStore


class ServerFSOAuthProvider:
    """Issue ServerFS-owned resource-bound opaque OAuth credentials."""

    def __init__(
        self,
        *,
        store: OAuthStateStore,
        issuer_url: str,
        resource_url: str,
        supported_scopes: tuple[str, ...] = ("serverfs:access",),
        access_token_ttl_seconds: int = 3600,
        refresh_token_ttl_seconds: int = 2_592_000,
    ) -> None:
        self.store = store
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = resource_url.rstrip("/")
        self.supported_scopes = supported_scopes
        self.access_token_ttl_seconds = access_token_ttl_seconds
        self.refresh_token_ttl_seconds = refresh_token_ttl_seconds

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.save_client(client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource is None or params.resource.rstrip("/") != self.resource_url:
            raise AuthorizeError(
                error="invalid_target",
                error_description="authorization request must target this ServerFS MCP resource",
            )
        scopes = list(params.scopes or self.supported_scopes)
        if not scopes or any(scope not in self.supported_scopes for scope in scopes):
            raise AuthorizeError(
                error="invalid_scope", error_description="unsupported ServerFS scope"
            )
        transaction = self.store.create_pending_authorization(
            client,
            params,
            scopes=scopes,
            resource=self.resource_url,
        )
        return f"{self.issuer_url}/login?{urlencode({'transaction': transaction})}"

    def complete_authorization(
        self, transaction: str, *, subject: str
    ) -> CompletedAuthorization | None:
        if not subject:
            return None
        return self.store.complete_pending_authorization(transaction, subject=subject)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self.store.load_authorization_code(client.client_id, authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        stored = self.store.consume_authorization_code(client.client_id, authorization_code.code)
        if stored is None:
            raise TokenError(
                error="invalid_grant", error_description="authorization code already used"
            )
        if stored.resource != self.resource_url or not stored.subject:
            raise TokenError(
                error="invalid_target", error_description="authorization code resource mismatch"
            )
        pair = self.store.issue_token_pair(
            client_id=client.client_id,
            scopes=stored.scopes,
            resource=self.resource_url,
            subject=stored.subject,
            issuer=self.issuer_url,
            access_ttl_seconds=self.access_token_ttl_seconds,
            refresh_ttl_seconds=self.refresh_token_ttl_seconds,
        )
        return OAuthToken(
            access_token=pair.access_token,
            token_type="Bearer",
            expires_in=self.access_token_ttl_seconds,
            scope=" ".join(stored.scopes),
            refresh_token=pair.refresh_token,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return self.store.load_refresh_token(client.client_id, refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        pair = self.store.rotate_refresh_token(
            client_id=client.client_id,
            token_value=refresh_token.token,
            scopes=scopes,
            issuer=self.issuer_url,
            access_ttl_seconds=self.access_token_ttl_seconds,
            refresh_ttl_seconds=self.refresh_token_ttl_seconds,
        )
        if pair is None:
            raise TokenError(
                error="invalid_grant", error_description="refresh token already used or invalid"
            )
        return OAuthToken(
            access_token=pair.access_token,
            token_type="Bearer",
            expires_in=self.access_token_ttl_seconds,
            scope=" ".join(scopes),
            refresh_token=pair.refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.store.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke_token_pair(token.token)
