"""Shared ServerFS HTTP proxy configuration.

This module owns the meaning of the four operator-facing ``SERVERFS_PROXY_*``
fields.  It is intentionally stdlib-only so deployment renderers can load the
file directly without importing the installed ``serverfs_mcp`` package.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

PROXY_KEYS = (
    "SERVERFS_PROXY_HOST",
    "SERVERFS_PROXY_PORT",
    "SERVERFS_PROXY_USERNAME",
    "SERVERFS_PROXY_PASSWORD",
)


@dataclass(frozen=True)
class SharedProxyConfig:
    """Validated shared HTTP proxy values.

    ``url`` may contain proxy credentials and therefore the repr is always
    redacted.  Callers must never log the URL or persist it outside an already
    private configuration boundary.
    """

    host: str
    port: int
    username: str
    password: str
    url: str

    @property
    def authenticated(self) -> bool:
        return bool(self.username)

    def __repr__(self) -> str:
        return f"SharedProxyConfig(authenticated={self.authenticated}, redacted=True)"


def parse_shared_proxy[E: Exception](
    values: Mapping[str, str],
    *,
    error_type: type[E] = ValueError,
) -> SharedProxyConfig | None:
    """Validate the four shared proxy fields and build the canonical HTTP URL.

    Empty HOST means no proxy and requires all companion fields to be empty.
    HOST accepts the existing ServerFS DNS/IPv4-shaped contract, PORT is
    normalized to 1..65535, PASSWORD requires USERNAME, and userinfo is
    percent-encoded byte-for-byte exactly as the v0.10 tunnel contract did.
    """

    host = values.get("SERVERFS_PROXY_HOST", "").strip()
    port = values.get("SERVERFS_PROXY_PORT", "").strip()
    username = values.get("SERVERFS_PROXY_USERNAME", "")
    password = values.get("SERVERFS_PROXY_PASSWORD", "")

    def fail(message: str) -> None:
        raise error_type(message)

    if not host:
        if port:
            fail("proxy PORT requires HOST")
        if username:
            fail("proxy USERNAME requires HOST")
        if password:
            fail("proxy PASSWORD requires HOST")
        return None
    if not port:
        fail("proxy HOST requires PORT")
    if (
        any(not (char.isascii() and (char.isalnum() or char in ".-")) for char in host)
        or host.startswith(".")
        or host.endswith((".", "-"))
        or ".." in host
        or "-." in host
        or ".-" in host
        or host.startswith("-")
    ):
        fail("proxy HOST must be a DNS name or IPv4 address")
    if not port.isascii() or not port.isdecimal():
        fail("proxy PORT must be an integer from 1 to 65535")
    normalized_port = int(port)
    if not 1 <= normalized_port <= 65535:
        fail("proxy PORT must be an integer from 1 to 65535")
    if password and not username:
        fail("proxy PASSWORD requires USERNAME")

    authority = f"{host}:{normalized_port}"
    if username:
        authority = f"{_percent_encode(username)}:{_percent_encode(password)}@{authority}"
    return SharedProxyConfig(
        host=host,
        port=normalized_port,
        username=username,
        password=password,
        url=f"http://{authority}",
    )


def _percent_encode(value: str) -> str:
    return "".join(f"%{byte:02x}" for byte in value.encode("utf-8"))
