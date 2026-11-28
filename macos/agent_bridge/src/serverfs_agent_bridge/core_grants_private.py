"""Owner-private Bridge v2 Core grant storage in an existing protected state home.

No implicit creation, repair, environment-variable secrets, key generation,
remote fetch or service activation. Explicit owner provisioning is a separate
operation from read-only startup. This file carries HMAC secrets: never log
its contents or include them in failures.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from typing import Any

from . import private_state
from .core_grants import CoreGrant, CoreGrantVerifier, _id, core_workdir_policy_digest
from .errors import BridgeError
from .policy import PolicyRegistry
from .store import TaskStore

_GRANTS_FILENAME = "core-grants.json"
_MAX_BYTES = 128 * 1024
_MAX_GRANTS = 32
_MAX_WORKDIRS = 16
_FIELDS = frozenset({"schema_version", "bridge_id", "grants"})
_GRANT_FIELDS = frozenset({"core_id", "key_id", "secret_hex", "methods", "workdirs"})
_WORKDIR_FIELDS = frozenset({"alias", "policy_sha256"})


def _deny() -> ValueError:
    # Neither key material, arbitrary JSON, path nor input values are shown.
    return ValueError("Bridge Core grants file is invalid or not private")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise _deny()
        result[key] = value
    return result


def _object(value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _deny()
    return value


def _parse_grants(data: Any, bridge_id: str, policies: PolicyRegistry) -> tuple[CoreGrant, ...]:
    root = _object(data, _FIELDS)
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise _deny()
    if root["bridge_id"] != bridge_id:
        raise _deny()
    entries = root["grants"]
    # An explicit empty array is the owner-persisted "revoke all" state.
    if not isinstance(entries, list) or len(entries) > _MAX_GRANTS:
        raise _deny()
    grants: list[CoreGrant] = []
    identities: set[tuple[str, str]] = set()
    try:
        for raw in entries:
            item = _object(raw, _GRANT_FIELDS)
            core_id = _id(item["core_id"])
            key_id = _id(item["key_id"])
            identity = (core_id, key_id)
            if identity in identities:
                raise _deny()
            identities.add(identity)
            secret_hex = item["secret_hex"]
            if (
                not isinstance(secret_hex, str)
                or len(secret_hex) != 64
                or any(ch not in "0123456789abcdef" for ch in secret_hex)
            ):
                raise _deny()
            methods = item["methods"]
            if (
                not isinstance(methods, list)
                or not 1 <= len(methods) <= 16
                or any(not isinstance(m, str) for m in methods)
                or len(set(methods)) != len(methods)
            ):
                raise _deny()
            dirs = item["workdirs"]
            if not isinstance(dirs, list) or not 1 <= len(dirs) <= _MAX_WORKDIRS:
                raise _deny()
            workdirs: list[tuple[str, str]] = []
            for d in dirs:
                mapping = _object(d, _WORKDIR_FIELDS)
                alias = mapping["alias"]
                supplied = mapping["policy_sha256"]
                if not isinstance(alias, str) or not isinstance(supplied, str):
                    raise _deny()
                actual = core_workdir_policy_digest(policies.get(alias))
                if supplied != actual:
                    raise _deny()
                workdirs.append((alias, actual))
            grants.append(
                CoreGrant(
                    core_id=core_id,
                    key_id=key_id,
                    secret=bytes.fromhex(secret_hex),
                    methods=frozenset(methods),
                    workdirs=tuple(workdirs),
                )
            )
    except (TypeError, KeyError, ValueError, AttributeError, BridgeError):
        raise _deny() from None
    return tuple(grants)


def load_private_core_grants(
    *,
    store: TaskStore,
    bridge_id: str,
    policies: PolicyRegistry,
) -> CoreGrantVerifier:
    """Build a v2 verifier from a private, already-existing operator grant file.

    The file may be installed/rotated by the separate instance owner only.
    This function verifies it; it never generates, rewrites or repairs it.
    """
    try:
        _id(bridge_id)
        path = store.state_dir / _GRANTS_FILENAME
        # TaskStore constructed this protected state home and verified it.
        # A fixed child filename prevents arbitrary file/path selection.
        fd = os.open(path, private_state.open_flags("read"))
    except (OSError, ValueError):
        raise _deny() from None
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size < 2
            or opened.st_size > _MAX_BYTES
            or not private_state.opened_file_is_private(path, opened)
        ):
            raise _deny()
        raw = os.read(fd, _MAX_BYTES + 1)
        if len(raw) != opened.st_size:
            raise _deny()
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        grants = _parse_grants(document, bridge_id, policies)
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, BridgeError):
        raise _deny() from None
    finally:
        os.close(fd)
    return CoreGrantVerifier(
        bridge_id,
        grants,
        nonce_claim=store.claim_core_nonce,
    )


def provision_private_core_grants(
    *, store: TaskStore, bridge_id: str, policies: PolicyRegistry, document: dict[str, Any]
) -> None:
    """Explicit owner-only atomic file installation; existing v1 is untouched.

    This does not activate or reload a running Bridge. An operator-controlled
    restart is required to consume new persisted grants. Concurrent writers
    require higher-level owner serialization; no central manager is added.
    """
    try:
        _id(bridge_id)
        _parse_grants(document, bridge_id, policies)
        raw = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
        if len(raw) > _MAX_BYTES:
            raise _deny()
        target = store.state_dir / _GRANTS_FILENAME
        # Replacing an existing foreign or reparse file is not an ACL repair.
        try:
            previous = os.open(target, private_state.open_flags("read"))
        except FileNotFoundError:
            pass
        else:
            try:
                previous_info = os.fstat(previous)
                if not private_state.opened_file_is_private(target, previous_info):
                    raise _deny()
            finally:
                os.close(previous)
    except (OSError, ValueError, TypeError, UnicodeError, BridgeError):
        raise _deny() from None

    staging = store.state_dir / (".core-grants-" + secrets.token_hex(16) + ".tmp")
    try:
        # Existing private-state primitives provide Windows explicit owner DACL
        # at creation and POSIX 0600; the candidate is random and same-dir.
        private_state.ensure_private_file(
            staging, mode=0o600, not_regular="private Core grants staging is unsafe"
        )
        fd = os.open(staging, private_state.open_flags("write"))
        try:
            opened = os.fstat(fd)
            if opened.st_size != 0 or not private_state.opened_file_is_private(staging, opened):
                raise _deny()
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise _deny()
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        # Both files reside in the private Bridge state home; replacement does
        # not introduce a public intermediate file or partial JSON document.
        os.replace(staging, target)
        load_private_core_grants(store=store, bridge_id=bridge_id, policies=policies)
    except (OSError, ValueError, TypeError, BridgeError):
        raise _deny() from None
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
