"""Client registration and Client ID Metadata Document (CIMD) support."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from mcp.shared.auth import OAuthClientInformationFull

_MAX_CIMD_BYTES = 64 * 1024
_DEFAULT_CIMD_CACHE_SECONDS = 300
_DEFAULT_CIMD_CACHE_ENTRIES = 128
_SUPPORTED_SCOPE = "serverfs:access"


class CimdError(ValueError):
    """A Client ID Metadata Document is invalid or could not be fetched safely."""


def validate_client_identifier_url(client_id: str) -> tuple[str, int, str]:
    """Validate the strict ServerFS CIMD URL subset and return host/port/path."""

    try:
        parsed = urlsplit(client_id)
        port = parsed.port or 443
    except ValueError as exc:
        raise CimdError("invalid Client Identifier URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or parsed.path in {"", "/"}
    ):
        raise CimdError("Client Identifier URL must be a stable non-root HTTPS URL")
    decoded_segments = [unquote(segment) for segment in parsed.path.split("/")]
    if any(segment in {".", ".."} for segment in decoded_segments):
        raise CimdError("Client Identifier URL must not contain dot path segments")
    return parsed.hostname, port, parsed.path


def validate_cimd_addresses(addresses: Sequence[str]) -> tuple[str, ...]:
    """Reject every special-use/non-global resolved address before connecting."""

    if not addresses:
        raise CimdError("Client Identifier URL did not resolve")
    checked: list[str] = []
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise CimdError("Client Identifier URL resolved to an invalid address") from exc
        if not address.is_global:
            raise CimdError("Client Identifier URL resolved to a non-global address")
        checked.append(address.compressed)
    return tuple(dict.fromkeys(checked))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection whose TCP destination is a previously validated IP address."""

    def __init__(self, host: str, port: int, connect_ip: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._connect_ip = connect_ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class CimdFetcher:
    """Fetch one CIMD document without redirects, proxies or DNS re-resolution."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 3.0,
        max_bytes: int = _MAX_CIMD_BYTES,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self._resolver = resolver

    def fetch(self, client_id: str) -> dict[str, Any]:
        hostname, port, path = validate_client_identifier_url(client_id)
        try:
            resolved = self._resolver(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CimdError("Client Identifier URL DNS resolution failed") from exc
        addresses = validate_cimd_addresses([str(item[4][0]) for item in resolved])
        connection = _PinnedHTTPSConnection(
            hostname,
            port,
            addresses[0],
            self.timeout_seconds,
        )
        host_header = hostname if port == 443 else f"{hostname}:{port}"
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "application/json, application/*+json",
                    "Host": host_header,
                    "User-Agent": "ServerFS-CIMD/0.16",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise CimdError("Client ID Metadata Document returned non-200 status")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json" and not (
                content_type.startswith("application/") and content_type.endswith("+json")
            ):
                raise CimdError("Client ID Metadata Document must be JSON")
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    if int(content_length) > self.max_bytes:
                        raise CimdError("Client ID Metadata Document is too large")
                except ValueError as exc:
                    raise CimdError("invalid Client ID Metadata Content-Length") from exc
            body = response.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise CimdError("Client ID Metadata Document is too large")
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise CimdError("Client ID Metadata Document fetch failed") from exc
        finally:
            connection.close()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CimdError("Client ID Metadata Document contains invalid JSON") from exc
        if not isinstance(payload, dict):
            raise CimdError("Client ID Metadata Document must be a JSON object")
        return payload


def client_from_cimd(client_id: str, payload: dict[str, Any]) -> OAuthClientInformationFull:
    """Validate a fetched CIMD document as a ServerFS public PKCE client."""

    if payload.get("client_id") != client_id:
        raise CimdError("Client ID Metadata Document client_id does not exactly match its URL")
    if "client_secret" in payload or "client_secret_expires_at" in payload:
        raise CimdError("CIMD shared-secret credentials are forbidden")
    if "jwks" in payload or "jwks_uri" in payload:
        raise CimdError("ServerFS v0.16 supports public CIMD clients only")
    if payload.get("token_endpoint_auth_method") != "none":
        raise CimdError("ServerFS CIMD clients must use token_endpoint_auth_method=none")

    metadata = dict(payload)
    metadata.setdefault("grant_types", ["authorization_code", "refresh_token"])
    metadata.setdefault("response_types", ["code"])
    metadata.setdefault("scope", _SUPPORTED_SCOPE)
    if "authorization_code" not in metadata["grant_types"]:
        raise CimdError("CIMD grant_types must include authorization_code")
    if "code" not in metadata["response_types"]:
        raise CimdError("CIMD response_types must include code")
    scopes = str(metadata.get("scope", "")).split()
    if not scopes or any(scope != _SUPPORTED_SCOPE for scope in scopes):
        raise CimdError("CIMD requests unsupported scopes")
    redirect_uris = metadata.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise CimdError("CIMD redirect_uris must be a non-empty list")
    try:
        return OAuthClientInformationFull.model_validate(metadata)
    except Exception as exc:
        raise CimdError("invalid Client ID Metadata Document") from exc


class ClientMetadataResolver:
    """Resolve and briefly cache valid CIMD public clients without persisting them."""

    def __init__(
        self,
        *,
        fetcher: CimdFetcher | None = None,
        cache_seconds: int = _DEFAULT_CIMD_CACHE_SECONDS,
        max_entries: int = _DEFAULT_CIMD_CACHE_ENTRIES,
    ) -> None:
        if cache_seconds <= 0 or max_entries <= 0:
            raise ValueError("CIMD cache bounds must be positive")
        self.fetcher = fetcher or CimdFetcher()
        self.cache_seconds = cache_seconds
        self.max_entries = max_entries
        self._cache: OrderedDict[str, tuple[float, OAuthClientInformationFull]] = OrderedDict()

    def resolve(self, client_id: str) -> OAuthClientInformationFull | None:
        try:
            validate_client_identifier_url(client_id)
        except CimdError:
            return None
        now = time.monotonic()
        cached = self._cache.get(client_id)
        if cached is not None:
            expires_at, client = cached
            if expires_at > now:
                self._cache.move_to_end(client_id)
                return client
            self._cache.pop(client_id, None)
        try:
            client = client_from_cimd(client_id, self.fetcher.fetch(client_id))
        except CimdError:
            return None
        self._cache[client_id] = (now + self.cache_seconds, client)
        self._cache.move_to_end(client_id)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)
        return client


def load_pre_registered_clients(path: str) -> list[OAuthClientInformationFull]:
    """Load an operator-managed JSON list of public pre-registered clients."""

    if not path:
        return []
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("failed to load SERVERFS_AUTH_CLIENTS_FILE") from exc
    if not isinstance(payload, list):
        raise ValueError("SERVERFS_AUTH_CLIENTS_FILE must contain a JSON array")
    clients: list[OAuthClientInformationFull] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("pre-registered client entry must be a JSON object")
        try:
            client = OAuthClientInformationFull.model_validate(item)
        except Exception as exc:
            raise ValueError("invalid pre-registered OAuth client") from exc
        if client.client_id in seen:
            raise ValueError(f"duplicate pre-registered client_id {client.client_id!r}")
        seen.add(client.client_id)
        if client.client_secret is not None or client.token_endpoint_auth_method != "none":
            raise ValueError("ServerFS v0.16 pre-registered clients must be public PKCE clients")
        if not client.redirect_uris:
            raise ValueError("pre-registered client requires redirect_uris")
        if "authorization_code" not in client.grant_types:
            raise ValueError("pre-registered client requires authorization_code grant")
        if "code" not in client.response_types:
            raise ValueError("pre-registered client requires code response type")
        scopes = (client.scope or _SUPPORTED_SCOPE).split()
        if not scopes or any(scope != _SUPPORTED_SCOPE for scope in scopes):
            raise ValueError("pre-registered client requests unsupported scopes")
        if client.scope is None:
            client.scope = _SUPPORTED_SCOPE
        clients.append(client)
    return clients
