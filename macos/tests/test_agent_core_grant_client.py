"""Pure Bridge protocol-v2 Core client tests: no network, Named Pipe or provider."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

from serverfs_mcp.agent_client import (
    AgentBridgeClient,
    AgentBridgeClientError,
    AgentBridgeInvalidRequest,
)
from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient

_SECRET = b"R" * 32
_POLICY = "a" * 64
_SOCKET = (
    Path(r"\\.\pipe\serverfs-v25-v2-unit")
    if sys.platform == "win32"
    else Path("/tmp/serverfs-v25-v2-unit")
)


def client(**override) -> CoreGrantBridgeClient:
    values = {
        "core_id": "core-a",
        "bridge_id": "shared",
        "key_id": "key-a",
        "secret": _SECRET,
        "policy_revisions": {"Repo": _POLICY},
        "server_pid_validator": (lambda pid: pid > 0) if sys.platform == "win32" else None,
    }
    values.update(override)
    return CoreGrantBridgeClient(_SOCKET, **values)


def invoke(client_object, method="repo.inspect", params=None):
    captured = []

    async def fake_exchange(raw, request_id):
        captured.append((raw, request_id))
        return {"accepted": True}

    client_object._exchange = fake_exchange
    args = {"workdir": "Repo", "view": "status"} if params is None else params
    result = asyncio.run(client_object.call(method, args))
    return result, captured


def test_signed_v2_request_has_exact_schema_and_canonical_hmac() -> None:
    result, calls = invoke(client())
    assert result == {"accepted": True}
    assert len(calls) == 1
    raw, request_id = calls[0]
    assert raw.endswith(b"\n")
    document = json.loads(raw)
    assert document["protocol_version"] == 2
    assert document["request_id"] == request_id
    assert document["core_id"] == "core-a"
    assert document["bridge_id"] == "shared"
    assert document["key_id"] == "key-a"
    assert document["policy_revision"] == _POLICY
    assert document["method"] == "repo.inspect"
    assert document["params"] == {"workdir": "Repo", "view": "status"}
    assert len(document["nonce"]) == 32
    signature = document.pop("signature")
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    expected = hmac.new(_SECRET, canonical, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)
    assert _SECRET not in raw


def test_each_call_has_a_fresh_nonce_and_request_identity() -> None:
    instance = client()
    captured = []

    async def fake_exchange(raw, request_id):
        captured.append(json.loads(raw))
        return {}

    instance._exchange = fake_exchange

    async def call_twice():
        await instance.call("repo.inspect", {"workdir": "Repo", "view": "status"})
        await instance.call("repo.inspect", {"workdir": "Repo", "view": "status"})

    asyncio.run(call_twice())
    assert len(captured) == 2
    assert captured[0]["nonce"] != captured[1]["nonce"]
    assert captured[0]["request_id"] != captured[1]["request_id"]


@pytest.mark.parametrize(
    "method",
    ["diagnostics.snapshot", "diagnostics.task"],
)
def test_unscoped_bridge_methods_are_never_signed(method: str) -> None:
    with pytest.raises(AgentBridgeClientError, match="not authorized"):
        invoke(client(), method)


def test_scoped_signed_bridge_health_probe_never_downgrades() -> None:
    result, sent = invoke(client(), "bridge.ping", {"workdir": "Repo"})
    assert result == {"accepted": True}
    envelope = json.loads(sent[0][0])
    assert envelope["protocol_version"] == 2
    assert envelope["params"] == {"workdir": "Repo"}
    assert envelope["method"] == "bridge.ping"
    with pytest.raises(AgentBridgeClientError):
        invoke(client(), "bridge.ping", {})


@pytest.mark.parametrize(
    "method",
    ["task.submit", "task.get", "task.events", "task.result.read", "task.cancel"],
)
def test_scoped_task_methods_are_signed_with_explicit_workdir(method: str) -> None:
    result, captured = invoke(client(), method, {"workdir": "Repo", "task_id": "agt_own"})
    assert result == {"accepted": True}
    body = json.loads(captured[0][0])
    assert body["method"] == method
    assert body["params"]["workdir"] == "Repo"
    assert body["policy_revision"] == _POLICY


def test_task_list_is_an_explicit_alias_scoped_rpc_not_a_task_id_lookup() -> None:
    result, captured = invoke(
        client(),
        "task.list",
        {"workdir": "Repo", "status": "active", "limit": 20},
    )
    assert result == {"accepted": True}
    body = json.loads(captured[0][0])
    assert body["method"] == "task.list"
    assert body["params"] == {"workdir": "Repo", "status": "active", "limit": 20}
    with pytest.raises(AgentBridgeClientError, match="named workdir"):
        invoke(client(), "task.list", {"status": "active"})


@pytest.mark.parametrize("key", ["continue_from_task_id", "idempotency_key"])
@pytest.mark.parametrize("value", ["private-value", ""])
def test_task_submission_rejects_unsupported_options_before_transport(key, value) -> None:
    instance = client()

    async def unexpected_exchange(raw, request_id):
        pytest.fail("unsupported submission must not reach transport")

    instance._exchange = unexpected_exchange
    with pytest.raises(AgentBridgeInvalidRequest, match="omit both") as caught:
        asyncio.run(instance.call("task.submit", {"workdir": "Repo", key: value}))
    assert "private-value" not in str(caught.value)


def test_task_submission_accepts_null_optional_fields() -> None:
    params = {"workdir": "Repo", "continue_from_task_id": None, "idempotency_key": None}
    result, sent = invoke(client(), "task.submit", params)
    assert result == {"accepted": True}
    assert json.loads(sent[0][0])["params"] == params


def test_unlisted_workdir_or_invalid_params_never_connect() -> None:
    for params in ({"workdir": "Other"}, {"view": "status"}, [], {"workdir": 3}):
        with pytest.raises(AgentBridgeClientError):
            invoke(client(), params=params)


@pytest.mark.parametrize(
    "option",
    [
        {"core_id": "../other"},
        {"bridge_id": "BAD"},
        {"key_id": "a" * 33},
        {"secret": b"short"},
        {"policy_revisions": {}},
        {"policy_revisions": {"bad/name": _POLICY}},
        {"policy_revisions": {"Repo": "bad-digest"}},
    ],
)
def test_untrusted_credentials_rejected_before_any_transport(option) -> None:
    with pytest.raises(ValueError):
        client(**option)


def test_windows_v2_refuses_missing_owner_generation_verifier() -> None:
    if sys.platform != "win32":
        pytest.skip("Windows named-pipe owner-generation proof is Windows-only")
    with pytest.raises(ValueError, match="owner-generation validator"):
        client(server_pid_validator=None)


def test_signed_payload_must_be_json_safe_and_bounded() -> None:
    with pytest.raises(AgentBridgeClientError, match="invalid signed"):
        invoke(client(), params={"workdir": "Repo", "view": float("nan")})
    with pytest.raises(AgentBridgeClientError, match="size"):
        invoke(client(), params={"workdir": "Repo", "message": "x" * 1_100_000})


def test_policy_mapping_is_immutable_after_construction() -> None:
    external = {"Repo": _POLICY}
    instance = client(policy_revisions=external)
    external["Repo"] = "b" * 64
    result, captured = invoke(instance)
    assert result == {"accepted": True}
    assert json.loads(captured[0][0])["policy_revision"] == _POLICY


def test_legacy_client_type_is_not_changed() -> None:
    assert issubclass(CoreGrantBridgeClient, AgentBridgeClient)
    instance = client()
    assert instance._secret == _SECRET
    assert AgentBridgeClient.call is not CoreGrantBridgeClient.call
