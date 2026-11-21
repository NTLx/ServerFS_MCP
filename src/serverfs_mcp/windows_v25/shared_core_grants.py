"""Pure owner-side issuance of independent Windows shared-Bridge v2 grants.

No storage, Scheduler, environment, IPC or implicit activation. The caller
must persist each Core binding only in its private Core home, provision the
Bridge grants via the installed Bridge worker's protected atomic writer, and
activate all owners only after their full policy/identity preflight passes.
Never log/render these secret-bearing documents outside the owner installer.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field

from serverfs_mcp.agent_core_grant_client import _METHODS
from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentity, valid_id

_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class SharedGrantIssueError(ValueError):
    """Fail without disclosing any generated private Core key material."""


def _deny() -> SharedGrantIssueError:
    return SharedGrantIssueError("independent shared-Bridge grant issuance refused")


@dataclass(frozen=True)
class IssuedCoreCapability:
    """A sealed-in-memory Core key. Secret excluded even from repr."""

    core_id: str
    bridge_id: str
    key_id: str
    workdirs: tuple[tuple[str, str], ...]
    methods: tuple[str, ...]
    secret: bytes = field(repr=False)

    def core_binding(self) -> dict:
        """Owner-only private Core JSON; caller must never print this dict."""
        return {
            "schema_version": 1,
            "instance_id": self.core_id,
            "bridge_mode": "shared",
            "bridge_id": self.bridge_id,
            "key_id": self.key_id,
            "secret_hex": self.secret.hex(),
            "workdirs": [
                {"alias": alias, "policy_sha256": digest} for alias, digest in self.workdirs
            ],
        }

    def bridge_grant(self) -> dict:
        """Bridge-worker private grant record; caller must never log it."""
        return {
            "core_id": self.core_id,
            "key_id": self.key_id,
            "secret_hex": self.secret.hex(),
            "methods": list(self.methods),
            "workdirs": [
                {"alias": alias, "policy_sha256": digest} for alias, digest in self.workdirs
            ],
        }


def issue_shared_core_capabilities(
    *,
    bridge_id: str,
    core_ids: tuple[str, ...],
    workdir_policy_revisions: Mapping[str, str],
    methods: tuple[str, ...],
) -> tuple[IssuedCoreCapability, ...]:
    """Issue independent keys; the Bridge must authoritatively supply digests.

    The operator prepares the exact Bridge policy first, obtains its full
    SHA-256 revisions through the installed Bridge package, and passes them
    here. This Core distribution never reimplements the Bridge digest.
    """
    try:
        valid_id(bridge_id)
        if bridge_id == DEFAULT_INSTANCE_ID or not isinstance(core_ids, tuple):
            raise _deny()
        if not 1 <= len(core_ids) <= 32 or len(set(core_ids)) != len(core_ids):
            raise _deny()
        for core in core_ids:
            if core in (DEFAULT_INSTANCE_ID, bridge_id):
                raise _deny()
            InstanceIdentity(core, "shared", bridge_id)
        if (
            not isinstance(workdir_policy_revisions, Mapping)
            or not 1 <= len(workdir_policy_revisions) <= 16
        ):
            raise _deny()
        scoped = tuple(sorted(workdir_policy_revisions.items()))
        if any(
            not isinstance(alias, str)
            or not _ALIAS.fullmatch(alias)
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
            for alias, digest in scoped
        ):
            raise _deny()
        if (
            not isinstance(methods, tuple)
            or not 1 <= len(methods) <= 16
            or len(set(methods)) != len(methods)
            or any(not isinstance(method, str) or method not in _METHODS for method in methods)
            or "bridge.ping" not in methods
        ):
            raise _deny()
        issued: list[IssuedCoreCapability] = []
        seen_keys: set[str] = set()
        seen_secrets: set[bytes] = set()
        for core in core_ids:
            key_id = "key-" + secrets.token_hex(8)
            secret = secrets.token_bytes(32)
            valid_id(key_id)
            if (
                type(secret) is not bytes
                or len(secret) != 32
                or key_id in seen_keys
                or secret in seen_secrets
            ):
                raise _deny()
            seen_keys.add(key_id)
            seen_secrets.add(secret)
            issued.append(IssuedCoreCapability(core, bridge_id, key_id, scoped, methods, secret))
        return tuple(issued)
    except (TypeError, ValueError, AttributeError):
        raise _deny() from None
