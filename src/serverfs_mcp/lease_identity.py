"""Platform-neutral workdir lease identity — the ServerFS half (v0.11 §5.3).

The mutation reader and the Bridge derive lease artifact names from the same logical lease id, and
the two packages must stay independent (§23), so this module mirrors
``serverfs_agent_bridge.lease_identity`` exactly. ``tests/test_lease_identity.py`` pins both sides
against one table of golden vectors: the shared object is the derivation, not the import.

A lease is keyed by a logical identifier, never by a path: a legacy Docker deployment keeps its
numeric slot, every other deployment is keyed by the exact workdir alias. The name shape follows
from the lease id kind, so there is no platform argument here.
"""

from __future__ import annotations

import hashlib
import re

from .errors import AgentLeaseError

SLOT_PREFIX = "slot:"
ALIAS_PREFIX = "alias:"
MAX_WORKDIR_SLOTS = 16
LOCK_SUFFIX = ".lock"
ARTIFACT_HASH_LENGTH = 40
MAX_LEASE_ID_LENGTH = 256

_SLOT_LEASE_RE = re.compile(r"^slot:([0-9]{2})$")
_UNSAFE_LEASE_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")


def slot_lease_id(slot: int) -> str:
    if type(slot) is not int or not 1 <= slot <= MAX_WORKDIR_SLOTS:
        raise AgentLeaseError("workdir slot must be between 1 and 16")
    return f"{SLOT_PREFIX}{slot:02d}"


def alias_lease_id(alias: str) -> str:
    # No case folding, no trimming, no separator normalization: the exact alias is the ServerFS
    # public identity, so the exact alias is what keys the lease. A colon is refused because the
    # prefix is what makes an id unambiguous, and an alias carrying one could impersonate a slot id.
    if not isinstance(alias, str) or not alias:
        raise AgentLeaseError("workdir alias must be a non-empty string")
    if ":" in alias:
        raise AgentLeaseError("workdir alias must not contain a lease prefix separator")
    if _UNSAFE_LEASE_CHARS.search(alias):
        raise AgentLeaseError("workdir alias contains a character that cannot key a lease")
    return f"{ALIAS_PREFIX}{alias}"


def validate_lease_id(lease_id: str) -> str:
    if not isinstance(lease_id, str) or not lease_id:
        raise AgentLeaseError("lease id must be a non-empty string")
    if len(lease_id) > MAX_LEASE_ID_LENGTH:
        raise AgentLeaseError("lease id is too long")
    if _UNSAFE_LEASE_CHARS.search(lease_id):
        raise AgentLeaseError("lease id contains a character that cannot key a lease")
    if not (lease_id.startswith(SLOT_PREFIX) or lease_id.startswith(ALIAS_PREFIX)):
        raise AgentLeaseError("lease id has an unknown kind")
    if lease_id.startswith(SLOT_PREFIX):
        match = _SLOT_LEASE_RE.match(lease_id)
        if match is None or not 1 <= int(match.group(1)) <= MAX_WORKDIR_SLOTS:
            raise AgentLeaseError("slot lease id is out of range")
    elif len(lease_id) == len(ALIAS_PREFIX):
        raise AgentLeaseError("alias lease id has an empty alias")
    return lease_id


def slot_of(lease_id: str) -> int | None:
    """The legacy numeric slot behind a lease id, or None for an alias-derived lease."""
    validate_lease_id(lease_id)
    match = _SLOT_LEASE_RE.match(lease_id)
    return None if match is None else int(match.group(1))


def _artifact_stem(lease_id: str) -> str:
    match = _SLOT_LEASE_RE.match(validate_lease_id(lease_id))
    if match is not None:
        # The v0.9/v0.10 layout stays byte-for-byte: a reader must still find the lock files a
        # running Bridge created under the old names.
        return match.group(1)
    return hashlib.sha256(lease_id.encode("utf-8")).hexdigest()[:ARTIFACT_HASH_LENGTH]


def lock_artifact_name(lease_id: str) -> str:
    return _artifact_stem(lease_id) + LOCK_SUFFIX


def guard_artifact_name(lease_id: str) -> str:
    return _artifact_stem(lease_id)
