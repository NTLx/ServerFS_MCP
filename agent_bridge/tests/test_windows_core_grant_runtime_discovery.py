"""Windows-native v2 dispatcher runtime discovery requires a signed Core+workdir policy.

No listener, provider, store, live session, Windows Job or native service.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_agent_bridge.core_grants import (
    CoreGrant,
    CoreGrantVerifier,
    core_workdir_policy_digest,
    sign_core_request,
)
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol_windows_native import BridgeProtocolServer

_SECRET = b"P" * 32
_ROOT = Path(__file__).resolve().parent
_REPO = WorkdirAgentPolicy(
    slot=None,
    alias="Repo",
    host_path=_ROOT,
    mode=AgentMode.WORKSPACE_WRITE,
    runtimes=frozenset({"codex"}),
    read_only=False,
)
_POLICY = core_workdir_policy_digest(_REPO)


def _server(methods: frozenset[str]):
    grant = CoreGrant("core-a", "key-a", _SECRET, (("Repo", _POLICY),), methods)
    result = object.__new__(BridgeProtocolServer)
    result._core_grants = CoreGrantVerifier("shared", (grant,))
    result.service = SimpleNamespace(policies=PolicyRegistry([_REPO]))
    calls = []

    async def dispatch(method, params):
        calls.append((method, params))
        return {"models": [], "status": "ok", "runtime": params.get("runtime", "")}

    async def list_runtimes():
        return {
            "runtimes": [
                {"name": "codex", "available": True},
                {"name": "claude", "available": True},
            ]
        }

    result._dispatch = dispatch
    result.service.list_runtimes = list_runtimes
    return result, calls


def _request(method: str, params: dict, nonce: str) -> dict:
    base = {
        "protocol_version": 2,
        "bridge_id": "shared",
        "core_id": "core-a",
        "key_id": "key-a",
        "request_id": "r_" + nonce[:8],
        "method": method,
        "params": params,
        "policy_revision": _POLICY,
        "issued_at": 1_800_000_000,
        "nonce": nonce,
    }
    return {**base, "signature": sign_core_request(base, _SECRET)}


def _send(server, request) -> dict:
    return asyncio.run(server._dispatch_raw(json.dumps(request).encode("utf-8")))


def _clock(monkeypatch):
    monkeypatch.setattr("serverfs_agent_bridge.core_grants.time.time", lambda: 1_800_000_000)


def test_signed_runtime_list_is_restricted_to_authorized_workdir(monkeypatch):
    _clock(monkeypatch)
    server, calls = _server(frozenset({"runtime.list"}))
    response = _send(server, _request("runtime.list", {"workdir": "Repo"}, "a" * 32))
    assert response["ok"] is True
    assert response["result"] == {"runtimes": [{"name": "codex", "available": True}]}
    assert not calls
    denied = _send(server, _request("runtime.list", {}, "b" * 32))
    assert denied["error"]["code"] == "CORE_NOT_AUTHORIZED"


def test_signed_runtime_models_plain_discovery_removes_only_scoping_key(monkeypatch):
    _clock(monkeypatch)
    server, calls = _server(frozenset({"runtime.models"}))
    ok = _send(
        server,
        _request("runtime.models", {"workdir": "Repo", "runtime": "codex"}, "c" * 32),
    )
    assert ok["ok"] is True
    assert calls == [("runtime.models", {"runtime": "codex"})]
    bad_runtime = _send(
        server,
        _request("runtime.models", {"workdir": "Repo", "runtime": "claude"}, "d" * 32),
    )
    assert bad_runtime["error"]["code"] == "CORE_NOT_AUTHORIZED"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "parameters",
    [
        {"workdir": "Repo", "runtime": "codex", "path": "x"},
        {"workdir": "Repo", "runtime": "codex", "profile": "review"},
        {"workdir": "Repo", "runtime": "codex", "task_prompt": 99},
    ],
)
def test_v2_models_refuses_implicit_or_malformed_advice(monkeypatch, parameters):
    _clock(monkeypatch)
    server, calls = _server(frozenset({"runtime.models"}))
    result = _send(server, _request("runtime.models", parameters, "e" * 32))
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert calls == []


def test_v2_runtime_discovery_is_unavailable_without_explicit_method_grant(monkeypatch):
    _clock(monkeypatch)
    server, calls = _server(frozenset({"repo.inspect"}))
    result = _send(server, _request("runtime.list", {"workdir": "Repo"}, "f" * 32))
    assert result["error"]["code"] == "CORE_NOT_AUTHORIZED"
    assert calls == []


def test_signed_runtime_discovery_refuses_stale_live_policy_before_dispatch(monkeypatch):
    _clock(monkeypatch)
    server, calls = _server(frozenset({"runtime.models"}))
    server.service.policies = PolicyRegistry(
        [
            WorkdirAgentPolicy(
                slot=None,
                alias="Repo",
                host_path=_ROOT,
                mode=AgentMode.DISABLED,
                read_only=True,
            )
        ]
    )
    result = _send(
        server,
        _request("runtime.models", {"workdir": "Repo", "runtime": "codex"}, "1" * 32),
    )
    assert result["error"]["code"] == "CORE_NOT_AUTHORIZED"
    assert calls == []
