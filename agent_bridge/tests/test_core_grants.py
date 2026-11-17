"""Protocol-v2 shared Bridge Core grants: pure non-operational security tests."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from serverfs_agent_bridge.core_grants import (
    CoreAuthorization,
    CoreGrant,
    CoreGrantVerifier,
    sign_core_request,
)
from serverfs_agent_bridge.errors import BridgeError

_SECRET_A = bytes(range(32))
_SECRET_B = bytes(range(32, 64))
_POLICY_A = "a" * 64
_POLICY_B = "b" * 64


def _grant(
    core_id: str = "alpha",
    *,
    key: bytes = _SECRET_A,
    key_id: str = "key-a",
    policy: str = _POLICY_A,
    alias: str = "Repo",
) -> CoreGrant:
    return CoreGrant(
        core_id,
        key_id,
        key,
        ((alias, policy),),
        frozenset({"repo.inspect"}),
    )


def _request(
    grant: CoreGrant,
    *,
    bridge_id: str = "bridge-x",
    method: str = "repo.inspect",
    workdir: str = "Repo",
    policy: str = _POLICY_A,
    issued_at: int = 1_800_000_000,
    nonce: str = "c" * 32,
) -> dict[str, Any]:
    unsigned = {
        "protocol_version": 2,
        "bridge_id": bridge_id,
        "core_id": grant.core_id,
        "key_id": grant.key_id,
        "request_id": "req-test",
        "method": method,
        "params": {"workdir": workdir, "view": "status"},
        "policy_revision": policy,
        "issued_at": issued_at,
        "nonce": nonce,
    }
    return {**unsigned, "signature": sign_core_request(unsigned, grant.secret)}


def _deny(verifier: CoreGrantVerifier, request: object, *, now: float = 1_800_000_000) -> None:
    with pytest.raises(BridgeError) as caught:
        verifier.authorize(request, now=now)
    assert caught.value.code == "CORE_NOT_AUTHORIZED"


def test_authorized_workdir_capability_and_nonce_replay_refused() -> None:
    grant = _grant()
    verifier = CoreGrantVerifier("bridge-x", (grant,))
    request = _request(grant)
    actual = verifier.authorize(request, now=1_800_000_000)
    assert actual == CoreAuthorization("alpha", "bridge-x", "Repo", "repo.inspect")
    _deny(verifier, request)
    # Fresh nonce with a valid MAC remains authorized.
    second = _request(grant, nonce="d" * 32)
    assert verifier.authorize(second, now=1_800_000_000).core_id == "alpha"


@pytest.mark.parametrize(
    "change",
    [
        {"bridge_id": "bridge-other"},
        {"core_id": "beta"},
        {"key_id": "key-b"},
        {"method": "repo.push"},
        {"policy_revision": _POLICY_B},
        {"nonce": "not-hex"},
        {"issued_at": 1_799_999_000},
        {"request_id": ""},
        {"protocol_version": 1},
    ],
)
def test_modified_signed_fields_are_rejected(change: dict[str, object]) -> None:
    grant = _grant()
    verifier = CoreGrantVerifier("bridge-x", (grant,))
    original = _request(grant)
    original.update(change)
    _deny(verifier, original)


def test_rejects_modified_nested_params_even_if_identity_is_valid() -> None:
    grant = _grant()
    request = _request(grant)
    request["params"]["view"] = "branches"
    _deny(CoreGrantVerifier("bridge-x", (grant,)), request)


@pytest.mark.parametrize(
    "method",
    [
        "runtime.list",
        "task.submit",
        "task.get",
        "task.events",
        "task.result.read",
        "task.cancel",
        "task.approval.respond",
        "task.question.answer",
        "diagnostics.snapshot",
        "bridge.ping",
    ],
)
def test_unscoped_task_or_bridge_methods_never_authorized(method: str) -> None:
    grant = _grant()
    request = _request(grant, method=method)
    _deny(CoreGrantVerifier("bridge-x", (grant,)), request)


def test_task_submit_requires_explicit_core_grant_and_refuses_continuation() -> None:
    grant = _grant()
    # No task permission can be inferred from a repo.inspect grant.
    _deny(CoreGrantVerifier("bridge-x", (grant,)), _request(grant, method="task.submit"))
    scoped = CoreGrant(
        grant.core_id,
        grant.key_id,
        grant.secret,
        grant.workdirs,
        frozenset({"repo.inspect", "task.submit"}),
    )
    assert (
        CoreGrantVerifier("bridge-x", (scoped,))
        .authorize(_request(scoped, method="task.submit"), now=1_800_000_000)
        .method
        == "task.submit"
    )
    # Re-signing a request with a valid Core key must not enable cross-Core
    # session continuation or the legacy global idempotency namespace.
    for key in ("continue_from_task_id", "idempotency_key"):
        req = _request(scoped, method="task.submit")
        req["params"][key] = "existing"
        unsigned = {name: value for name, value in req.items() if name != "signature"}
        req["signature"] = sign_core_request(unsigned, scoped.secret)
        _deny(CoreGrantVerifier("bridge-x", (scoped,)), req)


def test_multiple_cores_are_independent_even_with_the_same_nonce() -> None:
    a = _grant("alpha")
    b = _grant("beta", key=_SECRET_B, policy=_POLICY_B, alias="Other")
    verifier = CoreGrantVerifier("bridge-x", (a, b))
    assert verifier.authorize(_request(a), now=1_800_000_000).core_id == "alpha"
    assert (
        verifier.authorize(
            _request(b, workdir="Other", policy=_POLICY_B),
            now=1_800_000_000,
        ).core_id
        == "beta"
    )
    _deny(verifier, _request(b, workdir="Repo", policy=_POLICY_A, nonce="d" * 32))


def test_wrong_bridge_or_policy_blocks_replayed_authentication() -> None:
    grant = _grant()
    verifier = CoreGrantVerifier("bridge-x", (grant,))
    _deny(verifier, _request(grant, bridge_id="bridge-y"))
    _deny(verifier, _request(grant, policy=_POLICY_B))


def test_expired_and_future_time_fail_closed() -> None:
    grant = _grant()
    verifier = CoreGrantVerifier("bridge-x", (grant,))
    _deny(verifier, _request(grant), now=1_800_000_061)
    _deny(verifier, _request(grant), now=1_799_999_939)
    assert verifier.authorize(_request(grant), now=1_800_000_060).core_id == "alpha"


def test_nonce_cache_capacity_refuses_instead_of_eviction() -> None:
    grant = _grant()
    verifier = CoreGrantVerifier("bridge-x", (grant,), max_replay_entries=1)
    verifier.authorize(_request(grant), now=1_800_000_000)
    _deny(verifier, _request(grant, nonce="d" * 32))
    # On a later *valid* timestamp, the expired nonce can be evicted.
    third = _request(grant, issued_at=1_800_000_080, nonce="e" * 32)
    assert verifier.authorize(third, now=1_800_000_080).core_id == "alpha"


def test_malformed_envelope_rejected_without_secret_or_data_leakage() -> None:
    grant = _grant()
    verifier = CoreGrantVerifier("bridge-x", (grant,))
    base = _request(grant)
    for bad in (
        None,
        [],
        {},
        {**base, "unexpected": "text"},
        {key: value for key, value in base.items() if key != "signature"},
        {**base, "signature": 3},
        {**base, "params": {"workdir": 123}},
        {**base, "params": {"workdir": "Repo", "view": float("nan")}},
    ):
        _deny(verifier, bad)
    assert _SECRET_A.hex() not in repr(grant)
    assert "secret=b" not in repr(grant)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"core_id": "../escape"},
        {"key_id": "UPPERCASE"},
        {"key": b"short"},
        {"policy": "not-digest"},
        {"alias": "bad/name"},
    ],
)
def test_invalid_grants_rejected_before_any_registration(kwargs) -> None:
    with pytest.raises(ValueError):
        _grant(**kwargs)


def test_duplicate_grant_identity_or_alias_is_refused() -> None:
    grant = _grant()
    with pytest.raises(ValueError):
        CoreGrantVerifier("bridge-x", (grant, grant))
    with pytest.raises(ValueError):
        CoreGrant(
            "alpha",
            "key-a",
            _SECRET_A,
            (("Repo", _POLICY_A), ("Repo", _POLICY_B)),
            frozenset({"repo.inspect"}),
        )


def test_unsigned_or_malformed_proof_cannot_be_reused() -> None:
    grant = _grant()
    request = _request(grant)
    forged = copy.deepcopy(request)
    forged["signature"] = "0" * 64
    _deny(CoreGrantVerifier("bridge-x", (grant,)), forged)
    with pytest.raises(ValueError):
        sign_core_request({"unexpected": 1}, _SECRET_A)
