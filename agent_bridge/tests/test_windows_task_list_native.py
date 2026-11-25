"""Windows native signed task discovery against the real service and store."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path

import pytest

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.core_grants import (
    CoreGrant,
    CoreGrantVerifier,
    core_workdir_policy_digest,
    sign_core_request,
)
from serverfs_agent_bridge.lease_identity import NO_LEGACY_SLOT
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol_windows_native import BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore

_KEYS = {"key-a": b"A" * 32, "key-b": b"B" * 32, "key-rotated": b"C" * 32}


@pytest.fixture
def native(tmp_path: Path) -> BridgeProtocolServer:
    policies = []
    for alias in ("Agent", "Other", "Files", "NoRuntime"):
        root = tmp_path / alias
        root.mkdir()
        policies.append(
            WorkdirAgentPolicy(
                slot=None,
                alias=alias,
                host_path=root,
                mode=AgentMode.DISABLED if alias == "Files" else AgentMode.WORKSPACE_WRITE,
                runtimes=frozenset({"fake"}) if alias in {"Agent", "Other"} else frozenset(),
                read_only=alias == "Files",
            )
        )
    registry = PolicyRegistry(policies)
    store = TaskStore(tmp_path / "state")
    for task_id, owner, alias in (
        ("agt_a", "core-a", "Agent"),
        ("agt_b", "core-a", "Agent"),
        ("agt_foreign", "core-b", "Agent"),
        ("agt_other", "core-a", "Other"),
        ("agt_legacy", None, "Agent"),
    ):
        store.create_task(
            task_id=task_id,
            runtime="fake",
            workdir_alias=alias,
            workdir_slot=NO_LEGACY_SLOT,
            relative_cwd=".",
            profile="workspace-write",
            continue_from_task_id=None,
            owner_core_id=owner,
        )
    scope = tuple((p.alias, core_workdir_policy_digest(p)) for p in policies)
    methods = frozenset({"task.get", "task.list"})
    grants = (
        CoreGrant("core-a", "key-a", _KEYS["key-a"], scope, methods),
        CoreGrant("core-b", "key-b", _KEYS["key-b"], scope, methods),
        CoreGrant("core-a", "key-rotated", _KEYS["key-rotated"], scope, methods),
    )
    server = object.__new__(BridgeProtocolServer)
    server.service = BridgeService(
        store=store,
        policies=registry,
        adapters={"fake": FakeAdapter()},
        lease_manager=LeaseManager(tmp_path / "locks", lease_ids=registry.lease_ids()),
    )
    server._core_grants = CoreGrantVerifier("shared", grants)
    return server


def _signed(server, *, core="core-a", key="key-a", alias="Agent", params=None, revision=None):
    document = {
        "protocol_version": 2,
        "bridge_id": "shared",
        "core_id": core,
        "key_id": key,
        "request_id": "request-1",
        "method": "task.list",
        "params": {"workdir": alias, **(params or {})},
        "policy_revision": revision
        or core_workdir_policy_digest(server.service.policies.get(alias)),
        "issued_at": int(time.time()),
        "nonce": secrets.token_hex(16),
    }
    return {**document, "signature": sign_core_request(document, _KEYS[key])}


def _raw(server, document):
    return asyncio.run(server._dispatch_raw(json.dumps(document).encode("utf-8")))


def _call(server, **kwargs):
    return _raw(server, _signed(server, **kwargs))


def test_signed_discovery_scopes_and_paginates(native):
    first = _call(native, params={"limit": 1})
    assert first["ok"], first
    page = first["result"]
    assert len(page["tasks"]) == 1
    assert page["next_cursor"]
    second = _call(native, params={"limit": 1, "cursor": page["next_cursor"]})
    assert second["ok"], second
    assert second["result"]["next_cursor"] is None
    tasks = page["tasks"] + second["result"]["tasks"]
    assert {task["task_id"] for task in tasks} == {"agt_a", "agt_b"}
    assert all(task["correlation_id"] is None for task in tasks)
    other = _call(native, alias="Other")
    assert [t["task_id"] for t in other["result"]["tasks"]] == ["agt_other"]
    foreign = _call(native, core="core-b", key="key-b")
    assert [t["task_id"] for t in foreign["result"]["tasks"]] == ["agt_foreign"]
    for scope in (
        {"core": "core-b", "key": "key-b"},
        {"key": "key-rotated"},
        {"alias": "Other"},
    ):
        denied = _call(native, params={"cursor": page["next_cursor"]}, **scope)
        assert denied["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"status": "pending"}, "INVALID_REQUEST"),
        ({"status": None}, "INVALID_REQUEST"),
        ({"limit": 0}, "INVALID_LIMIT"),
        ({"limit": 101}, "INVALID_LIMIT"),
        ({"limit": True}, "INVALID_REQUEST"),
        ({"limit": "20"}, "INVALID_REQUEST"),
        ({"cursor": ""}, "INVALID_CURSOR"),
        ({"cursor": "x" * 2049}, "INVALID_CURSOR"),
        ({"cursor": 7}, "INVALID_REQUEST"),
        ({"task_id": "agt_a"}, "INVALID_REQUEST"),
        ({"core_id": "core-b"}, "INVALID_REQUEST"),
    ],
)
def test_invalid_parameters(native, params, code):
    response = _call(native, params=params)
    assert response["ok"] is False
    assert response["error"]["code"] == code


@pytest.mark.parametrize("alias", ["Files", "NoRuntime"])
def test_non_agent_policy_is_denied(native, alias):
    assert _call(native, alias=alias)["error"]["code"] == "CORE_NOT_AUTHORIZED"


def test_stale_policy_bad_signature_replay_and_revocation(native):
    assert _call(native, revision="0" * 64)["error"]["code"] == "CORE_NOT_AUTHORIZED"
    forged = _signed(native)
    forged["params"]["limit"] = 1
    assert _raw(native, forged)["error"]["code"] == "CORE_NOT_AUTHORIZED"
    request = _signed(native)
    assert _raw(native, request)["ok"]
    assert _raw(native, request)["error"]["code"] == "CORE_NOT_AUTHORIZED"
    native._core_grants.replace_grants(())
    assert _call(native)["error"]["code"] == "CORE_NOT_AUTHORIZED"


def test_legacy_discovery_fails_closed(native):
    native._core_grants = None
    request = {
        "protocol_version": 1,
        "request_id": "legacy",
        "method": "task.list",
        "params": {"workdir": "Agent"},
    }
    assert _raw(native, request)["error"]["code"] == "CORE_NOT_AUTHORIZED"
