"""OAuth Resource Server support for optional remote MCP authentication."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from mcp.server.auth.provider import AccessToken

from . import logging as jsonlog


class OAuthIntrospectionVerifier:
    """Verify ServerFS opaque bearer tokens through private introspection.

    The Resource Server never contacts upstream identity providers. It sends the
    opaque ServerFS token only to the configured private ``serverfs-auth``
    endpoint and converts an active response into the MCP SDK's ``AccessToken``.
    """

    def __init__(
        self,
        *,
        introspection_url: str,
        introspection_secret: str,
        timeout_seconds: float,
        expected_resource: str,
        expected_issuer: str,
    ) -> None:
        self._introspection_url = introspection_url
        self._introspection_secret = introspection_secret
        self._timeout_seconds = timeout_seconds
        self._expected_resource = expected_resource
        self._expected_issuer = expected_issuer
        # Explicitly ignore process HTTP(S)_PROXY for this private service hop.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        payload = await asyncio.to_thread(self._fetch_introspection, token)
        return self._access_token_from_payload(payload)

    def _fetch_introspection(self, token: str) -> Mapping[str, Any] | None:
        body = json.dumps({"token": token}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._introspection_url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._introspection_secret}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                if response.status != 200:
                    jsonlog.warning(
                        "oauth_introspection_failed",
                        reason="unexpected_status",
                        status=response.status,
                    )
                    return None
                raw = response.read()
        except urllib.error.HTTPError as exc:
            jsonlog.warning(
                "oauth_introspection_failed",
                reason="http_error",
                status=exc.code,
            )
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            jsonlog.warning("oauth_introspection_failed", reason="transport_error")
            return None

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            jsonlog.warning("oauth_introspection_failed", reason="invalid_json")
            return None
        if not isinstance(payload, dict):
            jsonlog.warning("oauth_introspection_failed", reason="invalid_response")
            return None
        return payload

    def _access_token_from_payload(self, payload: Mapping[str, Any] | None) -> AccessToken | None:
        if payload is None or payload.get("active") is not True:
            return None

        client_id = payload.get("client_id")
        subject = payload.get("sub")
        issuer = payload.get("iss")
        resource = payload.get("resource")
        scope = payload.get("scope")
        expires_at = payload.get("exp")

        if not isinstance(client_id, str) or not client_id:
            return None
        if not isinstance(subject, str) or not subject:
            return None
        if issuer != self._expected_issuer:
            return None
        if resource != self._expected_resource:
            return None
        if not isinstance(scope, str):
            return None
        scopes = scope.split()
        if not scopes or len(scopes) != len(set(scopes)):
            return None
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            return None
        if expires_at <= int(time.time()):
            return None

        return AccessToken(
            token="",
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=resource,
            subject=subject,
            claims={"iss": issuer},
        )
