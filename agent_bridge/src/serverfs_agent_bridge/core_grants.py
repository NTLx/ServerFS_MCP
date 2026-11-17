"""Pure v0.25 per-Core Bridge authorization contract; NOT activated.

A shared Bridge must authenticate a Core's distinct identity and bind each
request to an explicit workdir/policy grant. Legacy v1 clients are unchanged.
Task-ID RPCs require durable originating-Core ownership in the Bridge store.
The deployment owner, protected grant configuration and native v2 lifecycle
are still pending; nothing enables this protocol in production.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import BridgeError
from .policy import WorkdirAgentPolicy

_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32,64}\Z")
# Every v2 method requires a signed workdir; task methods additionally
# require the durable task-owner check at the Bridge protocol boundary.
_WORKDIR_METHODS = frozenset(
    {
        "bridge.ping",
        "runtime.list",
        "repo.inspect",
        "repo.action",
        "repo.commit",
        "repo.push",
        "runtime.models",
        "task.submit",
        "task.get",
        "task.events",
        "task.result.read",
        "task.cancel",
        "task.message.send",
        "task.approval.respond",
        "task.question.answer",
    }
)
_FIELDS = frozenset(
    {
        "protocol_version",
        "bridge_id",
        "core_id",
        "key_id",
        "request_id",
        "method",
        "params",
        "policy_revision",
        "issued_at",
        "nonce",
        "signature",
    }
)


def _deny() -> BridgeError:
    return BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")


def _id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid Core/Bridge ID")
    return value


def _sha256(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("invalid policy revision")
    return value


def core_workdir_policy_digest(policy: WorkdirAgentPolicy) -> str:
    """Hash the *full* authoritative Bridge policy, including the canonical host root.

    The legacy task-manifest fingerprint intentionally omits host_path and Repo
    Ops; it cannot be used to authorize a shared Core. This v2 digest covers
    the complete effective workdir and the allowlisted executable actions.
    No host paths or command arguments are returned or logged.
    """
    if not isinstance(policy, WorkdirAgentPolicy):
        raise ValueError("invalid Bridge workdir policy")
    document = {
        "alias": policy.alias,
        "host_path": os.path.normcase(str(policy.host_path)),
        "slot": policy.slot,
        "lease_id": policy.lease_id,
        "read_only": policy.read_only,
        "agent_mode": policy.mode.value,
        "agent_runtimes": sorted(policy.runtimes),
        "repo_ops_enabled": policy.repo_ops_enabled,
        "repo_actions": [
            asdict(action) for action in sorted(policy.repo_actions, key=lambda x: x.name)
        ],
    }
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CoreGrant:
    """Immutable operator-owned authorization for a single Core identity."""

    core_id: str
    key_id: str
    secret: bytes = field(repr=False)
    # (Bridge-owned alias, SHA256 of operator-approved policy) pairs
    workdirs: tuple[tuple[str, str], ...]
    methods: frozenset[str]

    def __post_init__(self) -> None:
        _id(self.core_id)
        _id(self.key_id)
        if not isinstance(self.secret, bytes) or len(self.secret) < 32:
            raise ValueError("independent Core secret must have at least 256 bits")
        if not isinstance(self.methods, frozenset) or not self.methods:
            raise ValueError("Core methods must be an explicit nonempty set")
        if not self.methods.issubset(_WORKDIR_METHODS):
            raise ValueError("Core method grants must be workdir-scoped")
        if not isinstance(self.workdirs, tuple) or not self.workdirs:
            raise ValueError("Core grant needs one or more workdir policies")
        names: set[str] = set()
        for alias, revision in self.workdirs:
            if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
                raise ValueError("Bridge workdir alias is invalid")
            _sha256(revision)
            if alias in names:
                raise ValueError("duplicate Bridge workdir grant")
            names.add(alias)

    def revision_for(self, alias: str) -> str | None:
        return next((digest for name, digest in self.workdirs if name == alias), None)


@dataclass(frozen=True)
class CoreAuthorization:
    core_id: str
    bridge_id: str
    workdir: str
    method: str


def canonical_message(envelope: Mapping[str, Any]) -> bytes:
    """Deterministic MAC input, including full params; never log the result."""
    if set(envelope) != _FIELDS - {"signature"}:
        raise ValueError("Core capability envelope fields differ")
    return json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def sign_core_request(envelope: Mapping[str, Any], secret: bytes) -> str:
    """Pure protocol helper; not a key store, RPC client, or authority."""
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Core secret is invalid")
    return hmac.new(secret, canonical_message(envelope), hashlib.sha256).hexdigest()


class CoreGrantVerifier:
    """One request at a time; no active RPC path uses this yet."""

    def __init__(
        self,
        bridge_id: str,
        grants: tuple[CoreGrant, ...],
        *,
        max_skew_seconds: int = 60,
        max_replay_entries: int = 4096,
        nonce_claim: Callable[[str, str, str, float, float], bool] | None = None,
    ) -> None:
        self.bridge_id = _id(bridge_id)
        if type(max_skew_seconds) is not int or not 1 <= max_skew_seconds <= 300:
            raise ValueError("invalid Core timestamp window")
        if type(max_replay_entries) is not int or max_replay_entries < 1:
            raise ValueError("invalid Core nonce cache limit")
        self.max_skew_seconds = max_skew_seconds
        self.max_replay_entries = max_replay_entries
        if nonce_claim is not None and not callable(nonce_claim):
            raise ValueError("Core nonce claim store must be callable")
        self._nonce_claim = nonce_claim
        self._grants: dict[tuple[str, str], CoreGrant] = {}
        self.replace_grants(grants)
        self._nonces: dict[tuple[str, str, str], float] = {}

    def replace_grants(self, grants: tuple[CoreGrant, ...]) -> None:
        """Atomically replace the operator-approved Core/key capabilities.

        An invalid update leaves the prior grants active. An empty tuple
        revokes all clients. The caller must persist the corresponding private
        configuration before trusting this runtime-only update after restart.
        Never reset durable nonce claims during a key rotation.
        """
        if not isinstance(grants, tuple):
            raise ValueError("Core grants must be an explicit tuple")
        candidate: dict[tuple[str, str], CoreGrant] = {}
        for grant in grants:
            if not isinstance(grant, CoreGrant):
                raise ValueError("invalid Core grant")
            identity = (grant.core_id, grant.key_id)
            if identity in candidate:
                raise ValueError("duplicate Core grant identity")
            candidate[identity] = grant
        self._grants = candidate

    @property
    def durable_replay_protection(self) -> bool:
        """Required before a protocol-v2 IPC listener is allowed to start."""
        return self._nonce_claim is not None

    def authorize(self, request: object, *, now: float | None = None) -> CoreAuthorization:
        try:
            return self._authorize(request, time.time() if now is None else now)
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise _deny() from exc

    def _authorize(self, request: object, now: float) -> CoreAuthorization:
        if not isinstance(request, dict) or set(request) != _FIELDS:
            raise _deny()
        if type(request["protocol_version"]) is not int or request["protocol_version"] != 2:
            raise _deny()
        bridge_id = _id(request["bridge_id"])
        core_id = _id(request["core_id"])
        key_id = _id(request["key_id"])
        if bridge_id != self.bridge_id:
            raise _deny()
        nonce = request["nonce"]
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            raise _deny()
        issued = request["issued_at"]
        if type(issued) is not int or not isinstance(now, (int, float)):
            raise _deny()
        if abs(now - issued) > self.max_skew_seconds:
            raise _deny()
        request_id = request["request_id"]
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise _deny()
        method = request["method"]
        params = request["params"]
        if method not in _WORKDIR_METHODS or not isinstance(params, dict):
            raise _deny()
        # v2 continuation and idempotency require a separate namespaced
        # protocol/store contract. No Core can borrow a v1 or peer Core task.
        if method == "task.submit" and (
            params.get("continue_from_task_id") is not None
            or params.get("idempotency_key") is not None
        ):
            raise _deny()
        workdir = params.get("workdir")
        if not isinstance(workdir, str):
            raise _deny()
        revision = _sha256(request["policy_revision"])
        grant = self._grants.get((core_id, key_id))
        if grant is None or method not in grant.methods or grant.revision_for(workdir) != revision:
            raise _deny()
        signature = _sha256(request["signature"])
        unsigned = {key: value for key, value in request.items() if key != "signature"}
        if not hmac.compare_digest(sign_core_request(unsigned, grant.secret), signature):
            raise _deny()
        if self._nonce_claim is not None:
            # Durable, atomic first-writer-wins across Bridge process restarts.
            # A storage outage or malformed result cannot degrade to a
            # volatile in-memory replay cache for deployed v2 endpoints.
            try:
                first_use = self._nonce_claim(
                    core_id, key_id, nonce, issued + self.max_skew_seconds, now
                )
            except Exception as exc:
                raise _deny() from exc
            if first_use is not True:
                raise _deny()
            return CoreAuthorization(core_id, bridge_id, workdir, method)
        # In-memory replay protection is for pure unit testing only.
        # BridgeProtocolServer.start refuses deployment without durable claims.
        for key, expires in tuple(self._nonces.items()):
            if expires < now:
                del self._nonces[key]
        replay_key = (core_id, key_id, nonce)
        if replay_key in self._nonces or len(self._nonces) >= self.max_replay_entries:
            raise _deny()
        self._nonces[replay_key] = issued + self.max_skew_seconds
        return CoreAuthorization(core_id, bridge_id, workdir, method)
