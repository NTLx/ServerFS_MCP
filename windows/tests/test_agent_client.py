"""Windows Core client wire contracts using the native Named Pipe boundary.

The Win32 handle is simulated here; real Pipe/Job/worker acceptance lives
in the native process tests. No global pipe, service or provider is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import windows_agent_pipe as transport
from serverfs_mcp.agent_client import (
    PROTOCOL_VERSION,
    AgentBridgeClient,
    AgentBridgeClientError,
    AgentBridgeRemoteError,
    AgentBridgeUnavailable,
)
from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient

PIPE = Path(r"\\.\pipe\serverfs-client-test")
SID = "fixture-user"
PID = 42
SECRET = bytes(range(32))


@pytest.fixture
def exchange(monkeypatch):
    """Exercise the actual client exchange and identity checks around a fake handle."""

    def install(handler, *, sid=SID, pid=PID):
        received = []
        closed = []

        class Pipe:
            def server_identity(self):
                return SimpleNamespace(user_sid=sid, process_id=pid)

            def write_all(self, encoded, *, deadline):
                assert encoded.endswith(b"\n")
                self.request = json.loads(encoded)
                received.append(self.request)

            def read_line(self, *, limit, deadline):
                return json.dumps(handler(self.request)).encode("utf-8") + b"\n"

            def close(self):
                closed.append(True)

        def open_pipe(name, *, deadline):
            assert name == str(PIPE)
            return Pipe()

        monkeypatch.setattr(transport.BridgePipe, "open", staticmethod(open_pipe))
        monkeypatch.setattr(transport, "current_user_sid", lambda: SID)
        return received, closed

    return install


def ok(request, result):
    return {"request_id": request["request_id"], "ok": True, "result": result}


def signed_client(**overrides):
    args = {
        "core_id": "core-blue",
        "bridge_id": "shared-one",
        "key_id": "key-blue",
        "secret": SECRET,
        "policy_revisions": {"Repo": "a" * 64},
        "server_pid_validator": lambda pid: pid == PID,
        "timeout_seconds": 1,
    }
    args.update(overrides)
    return CoreGrantBridgeClient(PIPE, **args)


def test_agent_bridge_client_round_trip(exchange):
    result = {"task_id": "agt_1", "status": "queued"}
    seen, closed = exchange(lambda request: ok(request, result))
    assert asyncio.run(AgentBridgeClient(PIPE).call("task.submit", {"runtime": "codex"})) == result
    assert seen[0]["protocol_version"] == PROTOCOL_VERSION
    assert seen[0]["method"] == "task.submit"
    assert seen[0]["params"] == {"runtime": "codex"}
    assert seen[0]["request_id"].startswith("mcp_")
    assert closed == [True]


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("diagnostics.snapshot", {}),
        ("diagnostics.task", {"task_id": "agt_local"}),
    ],
)
def test_agent_bridge_client_calls_private_diagnostics_rpc(exchange, method, params):
    result = {"catalog_version": 1, "disposition": "unknown_task"}
    seen, closed = exchange(lambda request: ok(request, result))
    assert asyncio.run(AgentBridgeClient(PIPE).call(method, params)) == result
    assert seen[0]["method"] == method
    assert seen[0]["params"] == params
    assert closed == [True]


@pytest.mark.parametrize("signed", [False, True])
def test_agent_bridge_remote_error_preserves_code(exchange, signed):
    def denied(request):
        return {
            "request_id": request["request_id"],
            "ok": False,
            "error": {"code": "WORKDIR_BUSY", "message": "workdir is busy"},
        }

    _, closed = exchange(denied)
    client = signed_client() if signed else AgentBridgeClient(PIPE)
    with pytest.raises(AgentBridgeRemoteError) as exc:
        asyncio.run(client.call("task.get", {"workdir": "Repo", "task_id": "agt_1"}))
    assert exc.value.code == "WORKDIR_BUSY"
    assert exc.value.message == "workdir is busy"
    assert closed == [True]


@pytest.mark.parametrize("signed", [False, True])
def test_agent_bridge_rejects_response_id_mismatch(exchange, signed):
    _, closed = exchange(lambda request: {"request_id": "wrong", "ok": True, "result": {}})
    client = signed_client() if signed else AgentBridgeClient(PIPE)
    with pytest.raises(AgentBridgeClientError, match="invalid response"):
        asyncio.run(client.call("bridge.ping", {"workdir": "Repo"}))
    assert closed == [True]


def test_agent_bridge_missing_pipe_is_unavailable(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("fixture")

    monkeypatch.setattr(transport.BridgePipe, "open", staticmethod(missing))
    with pytest.raises(AgentBridgeUnavailable, match="unavailable"):
        asyncio.run(AgentBridgeClient(PIPE).call("runtime.list", {}))


def test_signed_native_exchange_canonical_signature(exchange):
    seen, closed = exchange(lambda request: ok(request, {"pong": True}))
    assert asyncio.run(signed_client().call("bridge.ping", {"workdir": "Repo"})) == {"pong": True}
    assert len(seen) == 1
    request = dict(seen[0])
    signature = request.pop("signature")
    expected = hmac.new(
        SECRET,
        json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(signature, expected)
    assert request["protocol_version"] == 2
    assert request["params"] == {"workdir": "Repo"}
    assert len(request["nonce"]) == 32
    assert isinstance(request["issued_at"], int)
    assert closed == [True]


@pytest.mark.parametrize(("sid", "pid"), [("foreign-user", PID), (SID, PID + 1)])
def test_signed_client_rejects_wrong_owner_before_write(exchange, sid, pid):
    seen, closed = exchange(lambda request: pytest.fail("unexpected read"), sid=sid, pid=pid)
    with pytest.raises(AgentBridgeUnavailable):
        asyncio.run(signed_client().call("bridge.ping", {"workdir": "Repo"}))
    assert seen == []
    assert closed == [True]


def test_signed_client_repr_does_not_disclose_private_key():
    secret = b"s" * 32
    assert secret.decode() not in repr(signed_client(secret=secret))


def test_signed_timeout_is_unavailable_and_closes_pipe(exchange):
    def timeout(request):
        raise TimeoutError("fixture")

    seen, closed = exchange(timeout)
    with pytest.raises(AgentBridgeUnavailable, match="timed out"):
        asyncio.run(signed_client().call("bridge.ping", {"workdir": "Repo"}))
    assert len(seen) == 1
    assert closed == [True]


def test_signed_invalid_method_and_workdir_never_connect(exchange):
    seen, closed = exchange(lambda request: pytest.fail("unexpected read"))
    for method, params in (
        ("shell.exec", {"workdir": "Repo"}),
        ("bridge.ping", {"workdir": "Other"}),
    ):
        with pytest.raises(AgentBridgeClientError, match="not authorized"):
            asyncio.run(signed_client().call(method, params))
    assert not seen and not closed


def test_lost_submit_response_can_be_retried_with_same_idempotency_key(exchange):
    # This is the legacy v1 contract only: v2 still refuses non-null keys.
    tasks = {}

    def handler(request):
        key = request["params"]["idempotency_key"]
        if key not in tasks:
            tasks[key] = "agt_recovered"
            raise TimeoutError("response lost after commit")
        return ok(
            request,
            {"task_id": tasks[key], "status": "running", "idempotency_key": key},
        )

    seen, closed = exchange(handler)
    params = {
        "runtime": "codex",
        "workdir": "repo",
        "path": "",
        "profile": "workspace-write",
        "prompt": "one logical objective",
        "idempotency_key": "submit-1",
    }
    with pytest.raises(AgentBridgeUnavailable, match="timed out"):
        asyncio.run(AgentBridgeClient(PIPE).call("task.submit", params))
    recovered = asyncio.run(AgentBridgeClient(PIPE).call("task.submit", params))
    assert recovered["task_id"] == "agt_recovered"
    assert recovered["idempotency_key"] == "submit-1"
    assert len(seen) == 2
    assert tasks == {"submit-1": "agt_recovered"}
    assert closed == [True, True]


@pytest.mark.parametrize("foreign_platform", ("linux", "darwin", "freebsd"))
def test_platform_local_agent_client_rejects_foreign_host(monkeypatch, foreign_platform, tmp_path):
    from serverfs_mcp import agent_client

    monkeypatch.setattr(agent_client.sys, "platform", foreign_platform)
    with pytest.raises(ValueError, match="Agent Bridge client requires"):
        AgentBridgeClient(tmp_path / "bridge.sock")
