"""Derive the native macOS Bridge socket and lock directory independently.

The Core never imports the Bridge package. Injected endpoint and lock-directory
values may confirm, but cannot replace, the derived placement.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BRIDGE_DIRECTORY = "agent-bridge"
ENDPOINT_ENV = "SERVERFS_AGENT_BRIDGE_SOCKET"
LOCK_DIR_ENV = "SERVERFS_AGENT_LOCK_DIR"


class NativeEndpointError(Exception):
    """Redacted native endpoint or placement refusal."""


def data_home(env=None) -> Path:
    """Use the explicit override or the native Application Support directory."""
    environ = os.environ if env is None else env
    override = environ.get("SERVERFS_DATA_HOME", "").strip()
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "ServerFS"


def bridge_home(env=None) -> Path:
    """Where the native Bridge keeps its state, locks and generated config."""
    return data_home(env) / BRIDGE_DIRECTORY


def derive_lock_dir(env=None) -> Path:
    """The deterministic native lock directory: ``<data home>/agent-bridge/locks``."""
    return bridge_home(env) / "locks"


def derive_endpoint(env=None) -> str:
    """The macOS per-user runtime-directory AF_UNIX endpoint."""
    if sys.platform != "darwin":
        raise NativeEndpointError("the native Agent endpoint requires macOS")
    from .darwin_libc import darwin_bridge_socket_path

    return str(darwin_bridge_socket_path())


def _same_path(left: str, right: Path) -> bool:
    """Compare two lock-directory spellings without letting casing or separators decide."""
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def resolve_native_agent_wiring(env=None) -> tuple[str, Path]:
    """The endpoint and lock dir a direct serve must use, with injection treated as an override.

    Derived values are authoritative. When the supervisor injected the same values the result is
    identical; when it injected *different* ones the configuration is refused, because two
    derivation paths producing two addresses is precisely the failure the independent-derivation
    model exists to prevent.
    """
    environ = os.environ if env is None else env
    endpoint = derive_endpoint(environ)
    lock_dir = derive_lock_dir(environ)

    injected_endpoint = environ.get(ENDPOINT_ENV, "").strip()
    if injected_endpoint and os.path.normcase(injected_endpoint) != os.path.normcase(endpoint):
        raise NativeEndpointError(
            "the injected Agent Bridge endpoint disagrees with the endpoint derived from the "
            "current user; refusing rather than addressing a different Bridge than a direct serve "
            "would"
        )
    injected_lock_dir = environ.get(LOCK_DIR_ENV, "").strip()
    if injected_lock_dir and not _same_path(injected_lock_dir, lock_dir):
        raise NativeEndpointError(
            "the injected Agent lock directory disagrees with the location derived from the data "
            "home; refusing rather than using a different lease universe than a direct serve would"
        )
    return endpoint, lock_dir


__all__ = [
    "BRIDGE_DIRECTORY",
    "ENDPOINT_ENV",
    "LOCK_DIR_ENV",
    "NativeEndpointError",
    "bridge_home",
    "data_home",
    "derive_endpoint",
    "derive_lock_dir",
    "resolve_native_agent_wiring",
]
