from __future__ import annotations

import asyncio
from typing import Any

import pytest

from serverfs_mcp.agent_client import (
    PROTOCOL_VERSION,
    AgentBridgeClient,
    AgentBridgeClientError,
    AgentBridgeRemoteError,
    AgentBridgeUnavailable,
)
from serverfs_mcp.core_bridge_client_v2 import (
    CoreBridgeClientV2Error,
    CoreBridgeRemoteError,
    CoreBridgeUnavailable,
)
from serverfs_mcp.darwin_shared_agent_adapter import DarwinSharedAgentAdapter


class MockV2Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None
        self._secret = b"s" * 32

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if self.error:
            raise self.error
        return {"ok": True}


def test_workdir_alias_is_removed_before_v2_call_and_global_calls_pass() -> None:
    client = MockV2Client()
    adapter = DarwinSharedAgentAdapter(client, "Project")

    async def run() -> None:
        assert await adapter.call("task.get", {"task_id": "t1", "workdir": "Project"}) == {
            "ok": True
        }
        assert await adapter.call("task.get", {"task_id": "t2"}) == {"ok": True}
        assert await adapter.call("task.events", {"task_id": "t2", "after_event_id": 0}) == {
            "ok": True
        }
        assert await adapter.call("runtime.list", {}) == {"ok": True}
        assert await adapter.call("runtime.models", {"runtime": "codex"}) == {"ok": True}

    asyncio.run(run())
    assert client.calls == [
        ("task.get", {"task_id": "t1"}),
        ("task.get", {"task_id": "t2"}),
        ("task.events", {"task_id": "t2", "after_event_id": 0}),
        ("runtime.list", {}),
        ("runtime.models", {"runtime": "codex"}),
    ]


def test_cross_workdir_repo_ops_and_unknown_methods_are_rejected_before_v2_call() -> None:
    client = MockV2Client()
    adapter = DarwinSharedAgentAdapter(client, "Project")

    async def run() -> None:
        with pytest.raises(AgentBridgeClientError, match="outside the authorized grant"):
            await adapter.call("task.get", {"task_id": "t1", "workdir": "Other"})
        with pytest.raises(AgentBridgeClientError, match="Repo Ops"):
            await adapter.call("repo.inspect", {"workdir": "Project"})
        with pytest.raises(AgentBridgeClientError, match="unsupported"):
            await adapter.call("unknown.method", {})

    asyncio.run(run())
    assert client.calls == []


@pytest.mark.parametrize(
    ("core_error", "agent_error"),
    [
        (CoreBridgeRemoteError("NOT_FOUND", "task missing"), AgentBridgeRemoteError),
        (CoreBridgeUnavailable("private socket detail"), AgentBridgeUnavailable),
        (CoreBridgeClientV2Error("private protocol detail"), AgentBridgeClientError),
    ],
)
def test_v2_errors_map_to_agent_error_classes_without_private_details(
    core_error: Exception, agent_error: type[Exception]
) -> None:
    client = MockV2Client()
    client.error = core_error
    adapter = DarwinSharedAgentAdapter(client, "Project")

    async def run() -> None:
        with pytest.raises(agent_error) as caught:
            await adapter.call("runtime.list", {})
        assert "private" not in str(caught.value)
        assert "s" * 32 not in repr(adapter)

    asyncio.run(run())


def test_remote_error_message_is_bounded_and_secret_reflection_is_hidden() -> None:
    client = MockV2Client()
    client.error = CoreBridgeRemoteError("REMOTE_FAILURE", "leaked " + (b"s" * 32).decode())
    adapter = DarwinSharedAgentAdapter(client, "Project")

    async def run() -> None:
        with pytest.raises(AgentBridgeRemoteError) as caught:
            await adapter.call("runtime.list", {})
        assert str(client._secret) not in str(caught.value)
        assert caught.value.message == "remote request failed"

    asyncio.run(run())


def test_legacy_v1_client_contract_remains_available() -> None:
    assert PROTOCOL_VERSION == 1
    assert AgentBridgeClient is not None
