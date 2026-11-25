"""v2-only protocol dispatch stays isolated from the default v1 RPC path."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.core_grants import (
    CoreGrant,
    CoreGrantVerifier,
    core_workdir_policy_digest,
    sign_core_request,
)
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol import MacOSV2BridgeProtocolServer as BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore

_SECRET_A = bytes(range(32))
_SECRET_B = bytes(range(32, 64))


def _server(tmp_path: Path, *, v2_only: bool = True) -> tuple[BridgeProtocolServer, TaskStore]:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = WorkdirAgentPolicy(
        slot=1,
        alias="Repo",
        host_path=repo,
        mode=AgentMode.WORKSPACE_WRITE,
        runtimes=frozenset({"fake"}),
        read_only=False,
    )
    policies = PolicyRegistry([policy])
    store = TaskStore(tmp_path / "state")
    service = BridgeService(
        store=store,
        policies=policies,
        adapters={"fake": FakeAdapter()},
        lease_manager=LeaseManager(tmp_path / "locks", lease_ids=[policy.lease_id]),
    )
    claimed: set[tuple[str, str, str]] = set()

    def claim(core_id: str, key_id: str, nonce: str, expires_at: float, now: float) -> bool:
        del expires_at, now
        identity = (core_id, key_id, nonce)
        if identity in claimed:
            return False
        claimed.add(identity)
        return True

    digest = core_workdir_policy_digest(policy)
    grants = (
        CoreGrant(
            "alpha",
            "key-a",
            _SECRET_A,
            (("Repo", digest),),
            frozenset(
                {
                    "bridge.ping",
                    "repo.inspect",
                    "runtime.list",
                    "task.get",
                    "task.list",
                    "task.submit",
                }
            ),
        ),
        CoreGrant("beta", "key-b", _SECRET_B, (("Repo", digest),), frozenset({"task.get"})),
    )
    verifier = CoreGrantVerifier("bridge-x", grants, nonce_claim=claim)
    return (
        BridgeProtocolServer(
            service=service,
            socket_path=tmp_path / "bridge.sock",
            v2_only=v2_only,
            core_grant_verifier=verifier if v2_only else None,
        ),
        store,
    )


def _request(
    method: str = "runtime.list",
    *,
    core_id: str = "alpha",
    key_id: str = "key-a",
    secret: bytes = _SECRET_A,
    workdir: str = "Repo",
    policy_revision: str | None = None,
    nonce: str = "a" * 32,
    protocol_version: int = 2,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unsigned = {
        "protocol_version": protocol_version,
        "bridge_id": "bridge-x",
        "core_id": core_id,
        "key_id": key_id,
        "request_id": "request-1",
        "method": method,
        "params": {"workdir": workdir, **(params or {})},
        "policy_revision": policy_revision or "0" * 64,
        "issued_at": int(time.time()),
        "nonce": nonce,
    }
    if policy_revision is None:
        # The fixture's live policy digest is set by _server; tests replace this
        # sentinel after constructing the fixture using the helper below.
        unsigned["policy_revision"] = ""
    return {**unsigned, "signature": sign_core_request(unsigned, secret)}


def _signed(
    server: BridgeProtocolServer,
    method: str = "runtime.list",
    **kwargs: Any,
) -> dict[str, Any]:
    policy = server.service.policies.get("Repo")
    return _request(method, policy_revision=core_workdir_policy_digest(policy), **kwargs)


async def _dispatch(server: BridgeProtocolServer, request: dict[str, Any]) -> dict[str, Any]:
    return await server._dispatch_raw(json.dumps(request).encode() + b"\n")


@pytest.mark.asyncio
async def test_v2_rejects_tampered_signature_and_workdir_scope(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    tampered = _signed(server)
    tampered["params"]["extra"] = "tamper"
    assert (await _dispatch(server, tampered))["ok"] is False

    wrong_scope = _signed(server, workdir="Other", nonce="b" * 32)
    assert (await _dispatch(server, wrong_scope))["ok"] is False


@pytest.mark.asyncio
async def test_v2_nonce_replay_is_rejected(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    request = _signed(server)
    assert (await _dispatch(server, request))["ok"] is True
    replay = await _dispatch(server, request)
    assert replay["ok"] is False
    assert replay["error"]["code"] == "CORE_NOT_AUTHORIZED"


@pytest.mark.asyncio
async def test_v2_rejects_v1_downgrade_and_unsupported_method(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    downgrade = _signed(server, protocol_version=1)
    assert (await _dispatch(server, downgrade))["ok"] is False

    unsupported = _signed(server, "diagnostics.snapshot", nonce="c" * 32)
    assert (await _dispatch(server, unsupported))["ok"] is False


@pytest.mark.asyncio
async def test_v2_task_owner_requires_authenticated_core_and_workdir(tmp_path: Path) -> None:
    server, store = _server(tmp_path)
    store.create_task(
        task_id="agt_owner",
        runtime="fake",
        workdir_alias="Repo",
        workdir_slot=1,
        relative_cwd=".",
        profile="review",
        continue_from_task_id=None,
        owner_core_id="alpha",
    )

    cross_core = _signed(
        server,
        "task.get",
        core_id="beta",
        key_id="key-b",
        secret=_SECRET_B,
        nonce="d" * 32,
        params={"task_id": "agt_owner"},
    )
    assert (await _dispatch(server, cross_core))["ok"] is False
    wrong_workdir = _signed(
        server,
        "task.get",
        workdir="Other",
        nonce="e" * 32,
        params={"task_id": "agt_owner"},
    )
    assert (await _dispatch(server, wrong_workdir))["ok"] is False


@pytest.mark.asyncio
async def test_task_list_is_signed_core_and_alias_scoped_projection(tmp_path: Path) -> None:
    server, store = _server(tmp_path)
    store.create_task(
        task_id="agt_alpha",
        runtime="fake",
        workdir_alias="Repo",
        workdir_slot=1,
        relative_cwd="private/path",
        profile="review",
        continue_from_task_id=None,
        correlation_id=str(server.service.policies.get("Repo").host_path / "private-marker"),
        owner_core_id="alpha",
    )
    store.create_task(
        task_id="agt_beta",
        runtime="fake",
        workdir_alias="Repo",
        workdir_slot=1,
        relative_cwd="private/path",
        profile="review",
        continue_from_task_id=None,
        owner_core_id="beta",
    )
    policy = server.service.policies.get("Repo")
    writer_lease = server.service.lease_manager.acquire_exclusive(policy.lease_id)
    try:
        result = await _dispatch(
            server,
            _signed(
                server,
                "task.list",
                nonce="1" * 32,
                params={"status": "all", "limit": 1},
            ),
        )
    finally:
        writer_lease.release()
    assert result["ok"] is True, result
    page = result["result"]
    assert len(page["tasks"]) == 1
    assert page["tasks"][0]["task_id"] == "agt_alpha"
    assert page["tasks"][0]["correlation_id"] is None
    assert str(server.service.policies.get("Repo").host_path) not in repr(page)
    assert set(page["tasks"][0]) == {
        "task_id",
        "runtime",
        "workdir",
        "status",
        "created_at",
        "updated_at",
        "completed_at",
        "pending_interaction",
        "correlation_id",
    }
    assert page["next_cursor"] is None

    foreign = _signed(
        server,
        "task.list",
        core_id="beta",
        key_id="key-b",
        secret=_SECRET_B,
        nonce="2" * 32,
        params={"status": "all", "limit": 10},
    )
    assert (await _dispatch(server, foreign))["ok"] is False


@pytest.mark.asyncio
async def test_fresh_signed_client_recovers_active_and_terminal_ids(tmp_path: Path) -> None:
    server, store = _server(tmp_path)
    for task_id in ("agt_active", "agt_terminal"):
        store.create_task(
            task_id=task_id,
            runtime="fake",
            workdir_alias="Repo",
            workdir_slot=1,
            relative_cwd=".",
            profile="review",
            continue_from_task_id=None,
            owner_core_id="alpha",
        )
    store.transition_task("agt_terminal", "starting")
    store.transition_task("agt_terminal", "running")
    store.transition_task("agt_terminal", "succeeded")

    # The successor request carries no prior task ID; it learns each ID only from task.list.
    active_page = await _dispatch(
        server,
        _signed(server, "task.list", nonce="3" * 32, params={"status": "active"}),
    )
    terminal_page = await _dispatch(
        server,
        _signed(server, "task.list", nonce="4" * 32, params={"status": "terminal"}),
    )
    assert active_page["ok"] and terminal_page["ok"]
    discovered = [
        active_page["result"]["tasks"][0]["task_id"],
        terminal_page["result"]["tasks"][0]["task_id"],
    ]
    assert discovered == ["agt_active", "agt_terminal"]
    for index, task_id in enumerate(discovered, start=5):
        recovered = await _dispatch(
            server,
            _signed(server, "task.get", nonce=str(index) * 32, params={"task_id": task_id}),
        )
        assert recovered["ok"] is True
        assert recovered["result"]["task_id"] == task_id


@pytest.mark.asyncio
async def test_v2_submit_rejects_unsigned_or_spoofed_identity(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    request = _signed(
        server,
        "task.submit",
        nonce="f" * 32,
        params={
            "runtime": "fake",
            "path": ".",
            "profile": "review",
            "prompt": "do work",
        },
    )
    unsigned = dict(request)
    unsigned.pop("signature")
    assert (await _dispatch(server, unsigned))["ok"] is False
    request["core_id"] = "beta"
    assert (await _dispatch(server, request))["ok"] is False


@pytest.mark.asyncio
async def test_default_v1_dispatch_remains_compatible(tmp_path: Path) -> None:
    server, _ = _server(tmp_path, v2_only=False)
    response = await _dispatch(
        server,
        {
            "protocol_version": 1,
            "request_id": "legacy-1",
            "method": "runtime.list",
            "params": {},
        },
    )
    assert response["ok"] is True
    assert response["request_id"] == "legacy-1"
    discovery = await _dispatch(
        server,
        {
            "protocol_version": 1,
            "request_id": "legacy-list",
            "method": "task.list",
            "params": {"workdir": "Repo", "status": "all", "limit": 100},
        },
    )
    assert discovery["ok"] is False
    assert discovery["error"]["code"] == "CORE_NOT_AUTHORIZED"
