"""Non-operational Bridge v2 Core grant rotation and fail-closed revocation.

The runtime grant map can change only from trusted owner code; no RPC or
public control endpoint for grant changes exists. Persistence of the operator
configuration is a separate release gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_agent_bridge.core_grants import CoreGrantVerifier
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.store import TaskStore
from test_core_grants import _grant, _request

_NOW = 1_800_000_000.0


def _denied(verifier, request) -> None:
    with pytest.raises(BridgeError) as blocked:
        verifier.authorize(request, now=_NOW)
    assert blocked.value.code == "CORE_NOT_AUTHORIZED"


def test_two_keys_can_overlap_then_retire_old_key_without_nonce_reset(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "state")
    old = _grant("alpha", key_id="key-a", key=b"A" * 32)
    new = _grant("alpha", key_id="key-b", key=b"B" * 32)
    verifier = CoreGrantVerifier("bridge-x", (old,), nonce_claim=store.claim_core_nonce)
    assert verifier.authorize(_request(old, nonce="a" * 32), now=_NOW).core_id == "alpha"
    verifier.replace_grants((old, new))
    assert verifier.authorize(_request(new, nonce="b" * 32), now=_NOW).core_id == "alpha"
    verifier.replace_grants((new,))
    # Even a completely fresh signed old-key request must now fail.
    _denied(verifier, _request(old, nonce="c" * 32))
    # The new key retains access; the old key's durable nonce remains claimed.
    assert verifier.authorize(_request(new, nonce="d" * 32), now=_NOW).core_id == "alpha"
    assert not TaskStore(tmp_path / "state").claim_core_nonce(
        "alpha", "key-a", "a" * 32, _NOW + 60, _NOW
    )


def test_invalid_replacement_is_atomic_and_previous_grants_continue() -> None:
    old = _grant("alpha")
    verifier = CoreGrantVerifier("bridge-x", (old,))
    with pytest.raises(ValueError, match="duplicate"):
        verifier.replace_grants((old, old))
    with pytest.raises(ValueError, match="Core grants"):
        verifier.replace_grants([old])
    assert verifier.authorize(_request(old, nonce="e" * 32), now=_NOW).core_id == "alpha"


def test_empty_replacement_revokes_every_core_without_fallback() -> None:
    old = _grant("alpha")
    new = _grant("beta", key=b"B" * 32)
    verifier = CoreGrantVerifier("bridge-x", (old, new))
    verifier.replace_grants(())
    _denied(verifier, _request(old, nonce="f" * 32))
    _denied(verifier, _request(new, nonce="1" * 32))
