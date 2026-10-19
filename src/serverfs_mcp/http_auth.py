"""Static API Key authentication for the optional Streamable HTTP ingress."""

from __future__ import annotations

import hmac
from urllib.parse import parse_qsl

from starlette.types import ASGIApp, Receive, Scope, Send


class _InvalidCredential(ValueError):
    """Internal marker for missing, malformed or ambiguous credentials."""


def _authorization_values(scope: Scope) -> list[str]:
    return [
        value.decode("latin-1")
        for name, value in scope.get("headers", [])
        if name.lower() == b"authorization"
    ]


def _query_key_values(scope: Scope) -> list[str]:
    try:
        raw_query = scope.get("query_string", b"").decode("ascii")
        pairs = parse_qsl(
            raw_query,
            keep_blank_values=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise _InvalidCredential from exc
    return [value for name, value in pairs if name == "key"]


def _credential_from_scope(scope: Scope) -> str:
    authorization = _authorization_values(scope)
    query_keys = _query_key_values(scope)

    if len(authorization) > 1 or len(query_keys) > 1:
        raise _InvalidCredential
    if authorization and query_keys:
        raise _InvalidCredential

    if authorization:
        parts = authorization[0].split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            raise _InvalidCredential
        return parts[1]

    if query_keys and query_keys[0]:
        return query_keys[0]

    raise _InvalidCredential


async def _send_unauthorized(send: Send) -> None:
    body = b"Unauthorized"
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class StaticAPIKeyMiddleware:
    """Require one configured API Key before dispatching an HTTP request."""

    def __init__(self, app: ASGIApp, api_key: str) -> None:
        if not api_key:
            raise ValueError("StaticAPIKeyMiddleware requires a non-empty API key")
        self._app = app
        self._api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        try:
            candidate = _credential_from_scope(scope)
        except _InvalidCredential:
            await _send_unauthorized(send)
            return

        if not hmac.compare_digest(candidate, self._api_key):
            await _send_unauthorized(send)
            return

        await self._app(scope, receive, send)
