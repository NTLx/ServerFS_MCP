from __future__ import annotations

import asyncio
from typing import Any

import pytest

from helpers import call_error, call_success, registry_for
from serverfs_mcp.agent_client import AgentBridgeClientError, AgentBridgeRemoteError
from serverfs_mcp.config import Settings
from serverfs_mcp.core_bridge_client_v2 import CoreBridgeRemoteError
from serverfs_mcp.linux_v0250_bridge_adapter import LinuxV0250BridgeAdapter
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import AGENT_MODE_WORKSPACE_WRITE, EffectiveWorkdirPolicy, Workdir


class FakeV2Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._secret = b"s" * 32
        self._workdir_alias = "Project"

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        return {"ok": True}


def test_agent_and_repo_calls_pass_through_and_cross_grant_is_denied() -> None:
    client = FakeV2Client()
    adapter = LinuxV0250BridgeAdapter(client, "Project")

    async def run() -> None:
        assert await adapter.call("task.get", {"task_id": "t1"}) == {"ok": True}
        assert await adapter.call(
            "task.events", {"task_id": "t1", "after_event_id": 0, "limit": 5}
        ) == {"ok": True}
        assert await adapter.call("repo.push", {"workdir": "Project"}) == {"ok": True}
        with pytest.raises(AgentBridgeClientError, match="outside the authorized grant"):
            await adapter.call("repo.push", {"workdir": "Other"})
        with pytest.raises(AgentBridgeClientError, match="invalid Agent Bridge parameters"):
            await adapter.call("repo.push", {"workdir": "Project", "remote": "evil"})
        with pytest.raises(AgentBridgeClientError, match="unsupported"):
            await adapter.call("repo.force", {})

    asyncio.run(run())
    assert client.calls == [
        ("task.get", {"task_id": "t1"}),
        ("task.events", {"task_id": "t1", "after_event_id": 0, "limit": 5}),
        ("repo.push", {}),
    ]
    assert "s" * 32 not in repr(adapter)


def test_remote_error_is_sanitized() -> None:
    client = FakeV2Client()
    adapter = LinuxV0250BridgeAdapter(client, "Project")

    async def fail(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise CoreBridgeRemoteError("REMOTE_FAILURE", "secret " + "s" * 32)

    client.call = fail  # type: ignore[method-assign]
    with pytest.raises(AgentBridgeRemoteError) as caught:
        asyncio.run(
            adapter.call("repo.inspect", {"workdir": "Project", "view": "status", "paths": []})
        )
    assert caught.value.message == "remote request failed"
    assert "s" * 32 not in str(caught.value)


def test_create_server_registers_existing_agent_and_repo_tools_for_one_grant(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    wd = Workdir(
        alias="Project",
        root=root,
        description="test",
        read_only=False,
        agent_mode=AGENT_MODE_WORKSPACE_WRITE,
        agent_runtimes=frozenset({"codex"}),
        policy=EffectiveWorkdirPolicy(repo_ops_enabled=True),
    )
    fake = FakeV2Client()
    adapter = LinuxV0250BridgeAdapter(fake, "Project")
    server = create_server(
        Settings(agent_bridge_enabled=True, agent_lock_dir=str(tmp_path / "locks")),
        registry_for(wd),
        adapter,  # type: ignore[arg-type]
    )
    names = asyncio.run(server.list_tools())
    assert {item.name for item in names} >= {"submit_agent_task", "inspect_repo", "push_repo"}
    assert "repo.force" not in {item.name for item in names}
    assert "WORKDIR_NOT_FOUND" in call_error(server, "push_repo", {"workdir": "Other"})
    assert call_success(server, "push_repo", {"workdir": "Project"}) == {"ok": True}
    assert fake.calls[-1] == ("repo.push", {})
