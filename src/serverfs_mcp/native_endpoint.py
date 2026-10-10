r"""Deterministic native Agent endpoint derivation for the MCP side (v0.11 Phase D, §6.2, §4.2).

A direct ``serverfs serve`` is not a supervisor launcher: the Bridge lifecycle belongs to the
supervisor, so serve must never start one. But it still has to *address* one, and it must arrive at
the same address the supervisor would, without being told.

That is why this module exists. ``serverfs.toml`` stays the only operator source of Agent policy,
and the endpoint and lock directory are then **derived** from the frozen contract rather than
injected:

    current user SID -> sha256(SID.upper())[:16] -> \\.\pipe\serverfs-agent-bridge-v1-<hash>
    data home        -> <data home>\agent-bridge\locks

§23/§70 freeze the two packages as independent, so ``serverfs_agent_bridge`` is not imported here.
That is not a workaround: Phase B established that the two sides *must* derive the name
independently, because a deployment whose two sides disagree would connect to nothing while both
reported success. Independent derivation plus a parity test is the intended design, not a concession
— so the constants and algorithm are restated here and pinned to the Bridge's by test rather than by
a shared import.

Two rules keep internal wiring from becoming a second identity:

- a supervisor may inject ``SERVERFS_AGENT_BRIDGE_SOCKET`` / ``SERVERFS_AGENT_LOCK_DIR`` as
  defence-in-depth, but an injected value that disagrees with the derived one is a **startup
  refusal**, not an override. Letting internal env rewrite the operator's identity or layout would
  let a supervisor and a direct serve end up in two different lease/pipe universes.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

#: The frozen §6 pipe contract, restated for independent derivation (see the module docstring).
PIPE_NAMESPACE = "\\\\.\\pipe\\"
PIPE_NAME_PREFIX = PIPE_NAMESPACE + "serverfs-agent-bridge-v1-"
PIPE_NAME_HASH_LENGTH = 16

#: The Bridge data home is one directory below the ServerFS data home (§6.2).
BRIDGE_DIRECTORY = "agent-bridge"

#: Environment names the supervisor may use to inject wiring. Policy never comes from here.
ENDPOINT_ENV = "SERVERFS_AGENT_BRIDGE_SOCKET"
LOCK_DIR_ENV = "SERVERFS_AGENT_LOCK_DIR"


class NativeEndpointError(Exception):
    """The deterministic native Agent endpoint could not be derived.

    Operator-facing and redacted: it names the failure class, never a host path or a SID.
    """


def current_user_sid() -> str:
    """This process's own token SID, via the Phase B Windows transport helper.

    Reusing the helper the pipe client already uses for its server-SID assertion is deliberate: the
    value must be the same identity the transport will later compare against, or readiness would
    reject a Bridge this module addressed correctly.
    """
    from .windows_agent_pipe import current_user_sid as _sid

    return _sid()


def derive_pipe_name(user_sid: str) -> str:
    """The deterministic pipe name for one Windows user (§6).

    Mirrors ``serverfs_agent_bridge.local_ipc.derive_pipe_name`` exactly: a hash of the SID string,
    never the username, host name, PID or a random value, so the two sides derive the same name
    independently and two users' Bridges cannot collide. The name is disambiguation only — it is
    never an authentication factor (§4.4).
    """
    if not user_sid.startswith("S-"):
        raise NativeEndpointError("the native Agent endpoint requires a canonical user SID")
    digest = hashlib.sha256(user_sid.upper().encode("ascii")).hexdigest()
    return PIPE_NAME_PREFIX + digest[:PIPE_NAME_HASH_LENGTH]


def data_home(env=None) -> Path:
    """The frozen ServerFS data home for native deployments.

    Windows: ``SERVERFS_DATA_HOME`` or ``%LOCALAPPDATA%\\ServerFS``.
    macOS (v0.13): ``SERVERFS_DATA_HOME`` or
    ``~/Library/Application Support/ServerFS`` (§10 C2 — never the Linux
    XDG defaults on macOS).
    Linux: ``SERVERFS_DATA_HOME`` or XDG ``~/.local/share/serverfs``.
    """
    environ = os.environ if env is None else env
    override = environ.get("SERVERFS_DATA_HOME", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = environ.get("LOCALAPPDATA", "").strip()
        if not base:
            raise NativeEndpointError(
                "neither SERVERFS_DATA_HOME nor LOCALAPPDATA is set; the native Agent state "
                "location cannot be determined"
            )
        return Path(base) / "ServerFS"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ServerFS"
    xdg = environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / "serverfs"
    return Path.home() / ".local" / "share" / "serverfs"


def bridge_home(env=None) -> Path:
    """Where the native Bridge keeps its state, locks and generated config."""
    return data_home(env) / BRIDGE_DIRECTORY


def derive_lock_dir(env=None) -> Path:
    """The deterministic native lock directory: ``<data home>\\agent-bridge\\locks``."""
    return bridge_home(env) / "locks"


def derive_endpoint(env=None) -> str:
    """The deterministic native Bridge endpoint for the current user.

    Windows: the user-scoped pipe name (§6). Darwin (v0.13 §11 D2): the
    OS-provided per-user runtime directory holding a 0700
    ``serverfs-agent-bridge-v1/bridge.sock``, length-checked against the
    sun_path budget. Other platforms have no native derivation (the Linux
    Docker deployment passes its endpoint explicitly).
    """
    if sys.platform == "darwin":
        from .darwin_libc import darwin_bridge_socket_path

        return str(darwin_bridge_socket_path())
    return derive_pipe_name(current_user_sid())


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
    "PIPE_NAMESPACE",
    "PIPE_NAME_HASH_LENGTH",
    "PIPE_NAME_PREFIX",
    "NativeEndpointError",
    "bridge_home",
    "current_user_sid",
    "data_home",
    "derive_endpoint",
    "derive_lock_dir",
    "derive_pipe_name",
    "resolve_native_agent_wiring",
]
