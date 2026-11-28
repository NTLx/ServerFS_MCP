"""Platform-neutral workdir lease identity (v0.11 §5.3).

A lease is keyed by a *logical* identifier, never by a filesystem path. A legacy Docker deployment
keeps the numeric slot it was configured with; every other deployment is keyed by the exact workdir
alias. The artifact names produced here are the only part of that identity that reaches the
filesystem, and the Bridge that creates the artifacts and the ServerFS mutation reader that opens
them must derive them identically from the same string — which is why the derivation is a module of
its own rather than something each caller repeats.

There is deliberately no platform argument: the name shape follows from the lease id kind, so a
`slot:` lease keeps the historical `NN.lock` layout and an `alias:` lease is always hashed.
"""

from __future__ import annotations

import hashlib
import re

SLOT_PREFIX = "slot:"
ALIAS_PREFIX = "alias:"
MAX_WORKDIR_SLOTS = 16
LOCK_SUFFIX = ".lock"
# §5.5 item 1: 40 hex characters is bounded (45 with the suffix), collision-resistant, and immune to
# NTFS case folding, which would merge the artifacts of two aliases that differ only by case.
ARTIFACT_HASH_LENGTH = 40
MAX_LEASE_ID_LENGTH = 256
#: Stored in the legacy ``workdir_slot`` column for a workdir that has no numeric slot. The column
#: predates alias-keyed leases and stays a plain integer for every row already written; a task with
#: this value is keyed by its exact alias, exactly as ``Workdir.lease_id`` on the ServerFS side.
NO_LEGACY_SLOT = 0

_SLOT_LEASE_RE = re.compile(r"^slot:([0-9]{2})$")
_UNSAFE_LEASE_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")


class LeaseIdentityError(ValueError):
    """A lease identity is malformed; the caller must fail closed."""


def slot_lease_id(slot: int) -> str:
    if type(slot) is not int or not 1 <= slot <= MAX_WORKDIR_SLOTS:
        raise LeaseIdentityError("workdir slot must be between 1 and 16")
    return f"{SLOT_PREFIX}{slot:02d}"


def alias_lease_id(alias: str) -> str:
    # No case folding, no trimming, no separator normalization: the exact alias is the ServerFS
    # public identity, so it is the exact alias that keys the lease. A colon is refused because the
    # prefix is what makes an id unambiguous, and an alias carrying one could impersonate a slot id.
    if not isinstance(alias, str) or not alias:
        raise LeaseIdentityError("workdir alias must be a non-empty string")
    if ":" in alias:
        raise LeaseIdentityError("workdir alias must not contain a lease prefix separator")
    if _UNSAFE_LEASE_CHARS.search(alias):
        raise LeaseIdentityError("workdir alias contains a character that cannot key a lease")
    return f"{ALIAS_PREFIX}{alias}"


def validate_lease_id(lease_id: str) -> str:
    if not isinstance(lease_id, str) or not lease_id:
        raise LeaseIdentityError("lease id must be a non-empty string")
    if len(lease_id) > MAX_LEASE_ID_LENGTH:
        raise LeaseIdentityError("lease id is too long")
    if _UNSAFE_LEASE_CHARS.search(lease_id):
        raise LeaseIdentityError("lease id contains a character that cannot key a lease")
    if not (lease_id.startswith(SLOT_PREFIX) or lease_id.startswith(ALIAS_PREFIX)):
        raise LeaseIdentityError("lease id has an unknown kind")
    if lease_id.startswith(SLOT_PREFIX):
        match = _SLOT_LEASE_RE.match(lease_id)
        if match is None or not 1 <= int(match.group(1)) <= MAX_WORKDIR_SLOTS:
            raise LeaseIdentityError("slot lease id is out of range")
    elif len(lease_id) == len(ALIAS_PREFIX):
        raise LeaseIdentityError("alias lease id has an empty alias")
    return lease_id


def slot_of(lease_id: str) -> int | None:
    """The legacy numeric slot behind a lease id, or None for an alias-derived lease."""
    validate_lease_id(lease_id)
    match = _SLOT_LEASE_RE.match(lease_id)
    return None if match is None else int(match.group(1))


def describe(lease_id: str) -> str:
    """A human-readable label for messages, keeping the historical slot wording.

    The alias behind an alias-derived lease is the public workdir identity, so naming it in a
    message leaks nothing the caller could not already address.
    """
    slot = slot_of(lease_id)
    if slot is not None:
        return f"workdir slot {slot:02d}"
    return f"workdir {lease_id.removeprefix(ALIAS_PREFIX)}"


def _artifact_stem(lease_id: str) -> str:
    match = _SLOT_LEASE_RE.match(validate_lease_id(lease_id))
    if match is not None:
        # The v0.9/v0.10 layout stays byte-for-byte: an upgrade must still find the lock files and
        # guards that a running deployment created under the old names.
        return match.group(1)
    return hashlib.sha256(lease_id.encode("utf-8")).hexdigest()[:ARTIFACT_HASH_LENGTH]


def lock_artifact_name(lease_id: str) -> str:
    return _artifact_stem(lease_id) + LOCK_SUFFIX


def guard_artifact_name(lease_id: str) -> str:
    return _artifact_stem(lease_id)


def is_guard_artifact_name(name: str) -> bool:
    """Whether a directory entry is a guard artifact, for the recovery scan.

    Alias-derived guards cannot be enumerated by slot any more, so the guard directory is scanned.
    The stem shape is exact on purpose: a stray or partially written file must not be read as
    recovery state.
    """
    if len(name) == 2:
        return name.isdigit() and 1 <= int(name) <= MAX_WORKDIR_SLOTS
    if len(name) != ARTIFACT_HASH_LENGTH:
        return False
    return all(character in "0123456789abcdef" for character in name)
