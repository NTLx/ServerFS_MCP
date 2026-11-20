"""I7 interactive local-only Cloudflare Tunnel token import.

This helper is for a HUMAN at the native Windows console, never an Agent
task or redirected shell pipeline. Token text is accepted only via no-echo
getpass, validated in memory, and exclusively created in the matching
user-SID-protected I7 Connector home. No Cloudflare API access, service
start/stop, public DNS, credentials logging, or token command-line arguments.
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import importlib.util
import os
import re
import sys
from pathlib import Path

INSTANCE = "i7-public"
CONNECTORS = ("i7-edge-a", "i7-edge-b")
TOKEN_NAME = "tunnel.token"
PHASE = "entry"


class I7HandoffRefusal(RuntimeError):
    """Sanitized reason, no private path or token exposure."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I7HandoffRefusal(reason)


def loader():
    path = Path(__file__).with_name("v0250-i7-connectors-disabled-register.py")
    spec = importlib.util.spec_from_file_location("i7_token_handoff_registration", path)
    require(spec is not None and spec.loader is not None, "fixed registration code missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_user() -> None:
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated Windows Python -I -B required",
    )


def token_bytes(value: str) -> bytes:
    """Require known remote-management token transport syntax, not validity.

    Cryptographic authenticity and Tunnel ID binding are independently
    confirmed at Cloudflare account and live ingress acceptance.
    """
    require(
        isinstance(value, str)
        and value.isascii()
        and "\r" not in value
        and "\n" not in value
        and value == value.strip(),
        "provided credential format invalid",
    )
    data = value.encode("ascii")
    require(
        80 <= len(data) <= 4096
        and data.startswith(b"eyJ")
        and re.fullmatch(rb"[A-Za-z0-9_+=./-]+", data) is not None,
        "provided credential format invalid",
    )
    return data


def _locations(connector: str):
    from serverfs_mcp.windows_v25 import storage

    native_user()
    require(connector in CONNECTORS, "I7 Connector identity invalid")
    register = loader()
    require(
        register.INSTANCE == INSTANCE
        and register.IDS == CONNECTORS
        and set(register.EXPECTED_TUNNELS) == set(CONNECTORS),
        "I7 Connector registration identity differs",
    )
    register.base()  # protected I7 Core, owner, task-absence, production baseline
    root = storage.home("cloudflare", INSTANCE, connector)
    sid = storage._sid()
    storage.check_private(root, directory=True, sid=sid)
    token = root / TOKEN_NAME
    require(not token.exists() and not token.is_symlink(), "I7 credential already occupies slot")
    require(not any(root.iterdir()), "I7 Connector home is not empty")
    peer = next(name for name in CONNECTORS if name != connector)
    other = storage.home("cloudflare", INSTANCE, peer) / TOKEN_NAME
    peer_home = other.parent
    storage.check_private(peer_home, directory=True, sid=sid)
    require(
        all(path == other for path in peer_home.iterdir()),
        "I7 peer Connector home contains unexpected data",
    )
    if other.exists() or other.is_symlink():
        _hash, _account, peer_tunnel = register.private_token_record(other, sid=sid)
        require(
            peer_tunnel == register.EXPECTED_TUNNELS[peer],
            "I7 peer credential is for the wrong named Cloudflare Tunnel",
        )
    return register, token, other, sid


def interactive_console() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def import_private_token(connector: str, *, read_secret=getpass.getpass) -> None:
    global PHASE
    from serverfs_mcp.windows_v25 import storage

    PHASE = "protected-empty-home-audit"
    register, token, peer, sid = _locations(connector)
    # Reject typical noninteractive agent tasks and redirected input.
    # A TTY alone does not prove physical locality or operator identity;
    # run only from a trusted, attended Windows operator console.
    require(interactive_console(), "local interactive Windows console required")
    PHASE = "hidden-local-input"
    value = read_secret("Paste this I7 Tunnel Token locally (input hidden): ")
    data = token_bytes(value)
    del value
    if peer.exists() or peer.is_symlink():
        # Compare only in memory and never disclose either fingerprint.
        require(
            hashlib.sha256(data).digest() != register.private_token_digest(peer, sid=sid),
            "I7 Connector credential reused by peer",
        )
    PHASE = "create-only-protected-token-file"
    from serverfs_mcp.windows_private_artifacts import protect_new_file

    # The parent home was independently checked for an owner-protected DACL.
    # Open x+b cannot overwrite an existing file; no temp secret in workdir.
    created = False
    try:
        with token.open("xb") as stream:
            created = True
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        protect_new_file(token, sid)
        storage.check_private(token, directory=False, sid=sid)
        _hash, _account, tunnel_id = register.private_token_record(token, sid=sid)
        require(
            tunnel_id == register.EXPECTED_TUNNELS[connector],
            "I7 credential is for the wrong named Cloudflare Tunnel",
        )
    except BaseException:
        # Only remove the file this invocation successfully created; never
        # delete a pre-existing credential after an exclusive-open refusal.
        if created:
            token.unlink(missing_ok=True)
        raise
    finally:
        del data
    PHASE = "completed"
    print("I7_LOCAL_PRIVATE_TOKEN_IMPORTED=PASS")
    print("I7_TOKEN_NOT_LOGGED_OR_EXPORTED=PASS")
    print("I7_CONNECTOR_SERVICE_NOT_STARTED=PASS")


def audit() -> None:
    from serverfs_mcp.windows_v25 import storage

    native_user()
    register = loader()
    register.base()
    register.token_gate(allow_missing=True)
    for name in CONNECTORS:
        home = storage.home("cloudflare", INSTANCE, name)
        storage.check_private(home, directory=True)
    print("I7_LOCAL_CONSOLE_TOKEN_HANDOFF_AUDIT=PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true")
    group.add_argument("--import-connector", choices=CONNECTORS)
    args = parser.parse_args()
    if args.audit:
        audit()
    else:
        import_private_token(args.import_connector)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - credential and path redaction
        print(
            "I7_LOCAL_TOKEN_HANDOFF=FAILED phase=" + PHASE + " class=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
