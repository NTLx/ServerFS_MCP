"""Phase D Agent MCP tool surface and local authorization tests."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest
from mcp.server import MCPServer

from helpers import call_error, call_success, error_code, registry_for
from serverfs_mcp.agent_client import AgentBridgeRemoteError
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import (
    AGENT_MODE_DISABLED,
    AGENT_MODE_WORKSPACE_WRITE,
    Workdir,
    WorkdirRegistry,
)


class FakeAgentClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {
            "runtime.list": {
                "runtimes": [
                    {
                        "name": "codex",
                        "available": True,
                        "capabilities": {"persistent_session": True, "live_steer": True},
                    },
                    {
                        "name": "claude",
                        "available": True,
                        "capabilities": {"persistent_session": True, "live_steer": False},
                    },
                    {"name": "fake", "available": True},
                ]
            },
            "task.submit": {"task_id": "agt_test", "status": "queued"},
            "task.get": {"task_id": "agt_test", "status": "running"},
            "task.events": {"events": [], "next_after_event_id": 0},
            "task.approval.respond": {
                "task_id": "agt_test",
                "request_id": "req_1",
                "resolved": True,
            },
            "task.question.answer": {
                "task_id": "agt_test",
                "request_id": "req_q",
                "resolved": True,
            },
            "task.message.send": {"task_id": "agt_test", "accepted": True},
            "task.cancel": {"task_id": "agt_test", "status": "cancelled"},
        }
        self.error: AgentBridgeRemoteError | None = None

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.responses[method]


def agent_workdir(tmp_path: Path, *, enabled: bool = True) -> Workdir:
    root = tmp_path / "repo"
    root.mkdir()
    return Workdir(
        slot=1,
        alias="repo",
        container_path=root,
        description="Agent test workdir",
        read_only=not enabled,
        agent_mode=AGENT_MODE_WORKSPACE_WRITE if enabled else AGENT_MODE_DISABLED,
        agent_runtimes=frozenset({"codex", "claude"}) if enabled else frozenset(),
    )


def server_with_client(
    tmp_path: Path,
    client: FakeAgentClient,
    *,
    workdirs: list[Workdir] | None = None,
) -> MCPServer:
    """A server whose Agent surface is registered (at least one enabled workdir)."""
    wds = workdirs if workdirs is not None else [agent_workdir(tmp_path)]
    settings = Settings(
        agent_bridge_enabled=True,
        agent_bridge_socket="/tmp/test-agent-bridge.sock",
        agent_lock_dir=str(tmp_path / "locks"),
    )
    return create_server(settings, registry_for(*wds), client)  # type: ignore[arg-type]


def tool_names(server: MCPServer) -> set[str]:
    async def _get() -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    return asyncio.run(_get())


def test_default_server_surface_remains_v02_11_tools(tmp_path: Path) -> None:
    wd = agent_workdir(tmp_path, enabled=False)
    server = create_server(Settings(), WorkdirRegistry([wd]))
    names = tool_names(server)
    assert len(names) == 11
    assert "submit_agent_task" not in names
    assert "list_agent_runtimes" not in names


def test_enabled_server_requires_explicit_bridge_client(tmp_path: Path) -> None:
    wd = agent_workdir(tmp_path)
    settings = Settings(
        agent_bridge_enabled=True,
        agent_bridge_socket="/tmp/test-agent-bridge.sock",
        agent_lock_dir=str(tmp_path / "locks"),
    )
    with pytest.raises(ValueError, match="no client"):
        create_server(settings, WorkdirRegistry([wd]))


def test_enabled_surface_adds_exactly_eight_agent_tools(tmp_path: Path) -> None:
    client = FakeAgentClient()
    names = tool_names(server_with_client(tmp_path, client))
    expected = {
        "list_agent_runtimes",
        "submit_agent_task",
        "get_agent_task",
        "read_agent_task_events",
        "respond_agent_approval",
        "answer_agent_question",
        "send_agent_message",
        "cancel_agent_task",
    }
    assert expected <= names
    assert len(names) == 19


def test_agent_tool_schemas_and_annotations_are_frozen(tmp_path: Path) -> None:
    client = FakeAgentClient()
    server = server_with_client(tmp_path, client)

    async def _tools():
        return {tool.name: tool for tool in await server.list_tools()}

    tools = asyncio.run(_tools())
    expected_properties = {
        "list_agent_runtimes": set(),
        "submit_agent_task": {
            "runtime",
            "workdir",
            "prompt",
            "path",
            "profile",
            "continue_from_task_id",
        },
        "get_agent_task": {"task_id"},
        "read_agent_task_events": {"task_id", "after_event_id", "limit"},
        "respond_agent_approval": {
            "task_id",
            "request_id",
            "decision",
            "granted_permission_ids",
        },
        "answer_agent_question": {"task_id", "request_id", "answers"},
        "send_agent_message": {"task_id", "message"},
        "cancel_agent_task": {"task_id"},
    }
    expected_annotations = {
        "list_agent_runtimes": (True, None, None, False),
        "get_agent_task": (True, None, None, False),
        "read_agent_task_events": (True, None, None, False),
        "submit_agent_task": (False, True, False, True),
        "respond_agent_approval": (False, True, True, True),
        "answer_agent_question": (False, False, True, True),
        "send_agent_message": (False, True, False, True),
        "cancel_agent_task": (False, True, True, True),
    }

    for name, properties in expected_properties.items():
        tool = tools[name]
        assert set(tool.input_schema["properties"]) == properties
        assert tool.annotations is not None
        read_only, destructive, idempotent, open_world = expected_annotations[name]
        assert tool.annotations.read_only_hint is read_only
        assert tool.annotations.open_world_hint is open_world
        if destructive is not None:
            assert tool.annotations.destructive_hint is destructive
        if idempotent is not None:
            assert tool.annotations.idempotent_hint is idempotent


def test_submit_agent_task_guides_atomic_authorized_delegation(tmp_path: Path) -> None:
    client = FakeAgentClient()
    server = server_with_client(tmp_path, client)

    async def _tool():
        for tool in await server.list_tools():
            if tool.name == "submit_agent_task":
                return tool
        raise AssertionError("submit_agent_task not registered")

    tool = asyncio.run(_tool())
    prompt_description = tool.input_schema["properties"]["prompt"]["description"]
    assert "One narrow authorized objective" in prompt_description
    assert "allowed mutation scope" in prompt_description
    assert "structured project actions" in prompt_description
    assert tool.description is not None
    assert "Keep the task atomic" in tool.description
    assert "does not weaken or bypass provider safety checks" in tool.description


def test_send_agent_message_guides_narrow_follow_up(tmp_path: Path) -> None:
    client = FakeAgentClient()
    server = server_with_client(tmp_path, client)

    async def _tool():
        for tool in await server.list_tools():
            if tool.name == "send_agent_message":
                return tool
        raise AssertionError("send_agent_message not registered")

    tool = asyncio.run(_tool())
    message_description = tool.input_schema["properties"]["message"]["description"]
    assert "Narrow follow-up" in message_description
    assert "without expanding its authorized scope" in message_description
    assert tool.description is not None
    assert "existing authorized objective" in tool.description
    assert "submit a new atomic task instead" in tool.description


def test_global_enable_without_agent_workdir_keeps_eleven_tool_surface(
    tmp_path: Path,
) -> None:
    client = FakeAgentClient()
    wd = agent_workdir(tmp_path, enabled=False)
    settings = Settings(
        agent_bridge_enabled=True,
        agent_bridge_socket="/tmp/test-agent-bridge.sock",
        agent_lock_dir=str(tmp_path / "locks"),
    )
    server = create_server(settings, WorkdirRegistry([wd]), client)  # type: ignore[arg-type]
    names = tool_names(server)
    assert len(names) == 11
    assert "list_agent_runtimes" not in names
    assert client.calls == []


def test_list_agent_runtimes_filters_non_public_or_unallowlisted_runtime(tmp_path: Path) -> None:
    client = FakeAgentClient()
    server = server_with_client(tmp_path, client)
    result = call_success(server, "list_agent_runtimes", {})
    assert [item["name"] for item in result["runtimes"]] == ["codex", "claude"]
    assert client.calls == [("runtime.list", {})]


def test_submit_agent_task_maps_flat_arguments_to_bridge_rpc(tmp_path: Path) -> None:
    client = FakeAgentClient()
    wd = agent_workdir(tmp_path)
    (wd.container_path / "src").mkdir()
    server = server_with_client(tmp_path, client, workdirs=[wd])

    result = call_success(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": "repo",
            "path": "src",
            "profile": "workspace-write",
            "prompt": "Fix the failing test",
        },
    )
    assert result == {"task_id": "agt_test", "status": "queued"}
    assert client.calls[-1] == (
        "task.submit",
        {
            "runtime": "codex",
            "workdir": "repo",
            "path": "src",
            "profile": "workspace-write",
            "prompt": "Fix the failing test",
        },
    )


def test_submit_continuation_forwards_prior_task_id(tmp_path: Path) -> None:
    client = FakeAgentClient()
    server = server_with_client(tmp_path, client)
    call_success(
        server,
        "submit_agent_task",
        {
            "runtime": "claude",
            "workdir": "repo",
            "profile": "workspace-write",
            "prompt": "Continue",
            "continue_from_task_id": "agt_prior",
        },
    )
    assert client.calls[-1][1]["continue_from_task_id"] == "agt_prior"


def test_submit_rejects_disabled_workdir_before_rpc(tmp_path: Path) -> None:
    client = FakeAgentClient()
    disabled = agent_workdir(tmp_path, enabled=False)
    # A second, enabled workdir: the Agent surface is only registered when one
    # exists, and this test is about the per-workdir authorization refusal.
    reachable = Workdir(
        slot=2,
        alias="open",
        container_path=tmp_path / "open",
        description=None,
        read_only=False,
        agent_mode=AGENT_MODE_WORKSPACE_WRITE,
        agent_runtimes=frozenset({"codex"}),
    )
    server = server_with_client(tmp_path, client, workdirs=[disabled, reachable])
    message = call_error(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": "repo",
            "profile": "workspace-write",
            "prompt": "Do work",
        },
    )
    assert error_code(message) == "AGENT_DISABLED"
    assert client.calls == []


def test_submit_rejects_unknown_workdir_before_rpc(tmp_path: Path) -> None:
    client = FakeAgentClient()
    server = server_with_client(tmp_path, client)
    message = call_error(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": "nope",
            "profile": "workspace-write",
            "prompt": "Do work",
        },
    )
    assert error_code(message) == "WORKDIR_NOT_FOUND"
    assert client.calls == []


def test_submit_rejects_runtime_not_allowlisted_on_workdir(tmp_path: Path) -> None:
    client = FakeAgentClient()
    original = agent_workdir(tmp_path)
    wd = dataclasses.replace(
        original,
        policy=dataclasses.replace(original.policy, agent_runtimes=frozenset({"claude"})),
    )
    server = server_with_client(tmp_path, client, workdirs=[wd])
    message = call_error(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": "repo",
            "profile": "workspace-write",
            "prompt": "Do work",
        },
    )
    assert error_code(message) == "AGENT_RUNTIME_NOT_ALLOWED"
    assert client.calls == []


def test_submit_rejects_starting_path_that_is_not_a_directory(tmp_path: Path) -> None:
    client = FakeAgentClient()
    wd = agent_workdir(tmp_path)
    (wd.container_path / "notes.txt").write_text("a file, not a directory\n")
    server = server_with_client(tmp_path, client, workdirs=[wd])
    message = call_error(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": "repo",
            "path": "notes.txt",
            "profile": "workspace-write",
            "prompt": "Do work",
        },
    )
    assert error_code(message) == "NOT_A_DIRECTORY"
    assert client.calls == []


def test_native_runtime_review_profile_rejected_before_rpc(tmp_path: Path) -> None:
    client = FakeAgentClient()
    server = server_with_client(tmp_path, client)
    message = call_error(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": "repo",
            "profile": "review",
            "prompt": "Review only",
        },
    )
    assert error_code(message) == "AGENT_PROFILE_NOT_ALLOWED"
    assert client.calls == []


def test_agent_starting_cwd_uses_normal_serverfs_path_policy(tmp_path: Path) -> None:
    client = FakeAgentClient()
    wd = agent_workdir(tmp_path)
    (wd.container_path / ".hidden").mkdir()
    server = server_with_client(tmp_path, client, workdirs=[wd])
    message = call_error(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": "repo",
            "path": ".hidden",
            "profile": "workspace-write",
            "prompt": "Do work",
        },
    )
    assert error_code(message) == "HIDDEN_PATH_NOT_ALLOWED"
    assert client.calls == []


def test_agent_starting_cwd_uses_selected_workdir_policy(tmp_path: Path) -> None:
    client = FakeAgentClient()
    wd = agent_workdir(tmp_path)
    (wd.container_path / ".hidden").mkdir()
    wd = dataclasses.replace(wd, policy=dataclasses.replace(wd.policy, allow_hidden=True))
    server = server_with_client(tmp_path, client, workdirs=[wd])
    result = call_success(
        server,
        "submit_agent_task",
        {
            "runtime": "codex",
            "workdir": "repo",
            "path": ".hidden",
            "profile": "workspace-write",
            "prompt": "Do work",
        },
    )
    assert result["task_id"] == "agt_test"
    assert client.calls[-1][0] == "task.submit"
    assert client.calls[-1][1]["path"] == ".hidden"


def test_read_and_interaction_tools_map_to_expected_rpc_methods(tmp_path: Path) -> None:
    client = FakeAgentClient()
    server = server_with_client(tmp_path, client)

    call_success(server, "get_agent_task", {"task_id": "agt_test"})
    call_success(
        server,
        "read_agent_task_events",
        {"task_id": "agt_test", "after_event_id": 3, "limit": 25},
    )
    call_success(
        server,
        "respond_agent_approval",
        {
            "task_id": "agt_test",
            "request_id": "req_1",
            "decision": "approve_once",
        },
    )
    call_success(
        server,
        "answer_agent_question",
        {
            "task_id": "agt_test",
            "request_id": "req_q",
            "answers": [
                {
                    "question_id": "q0",
                    "selected_option_ids": ["B"],
                    "text": "details",
                }
            ],
        },
    )
    call_success(
        server,
        "send_agent_message",
        {"task_id": "agt_test", "message": "Focus on the API layer"},
    )
    call_success(server, "cancel_agent_task", {"task_id": "agt_test"})

    assert [method for method, _ in client.calls] == [
        "task.get",
        "task.events",
        "task.approval.respond",
        "task.question.answer",
        "task.message.send",
        "task.cancel",
    ]
    assert client.calls[3][1]["answers"][0] == {
        "question_id": "q0",
        "selected_option_ids": ["B"],
        "text": "details",
    }


def test_bridge_remote_error_is_preserved_as_coded_tool_error(tmp_path: Path) -> None:
    client = FakeAgentClient()
    client.error = AgentBridgeRemoteError("WORKDIR_BUSY", "workdir is busy")
    server = server_with_client(tmp_path, client)
    message = call_error(server, "get_agent_task", {"task_id": "agt_test"})
    assert error_code(message) == "WORKDIR_BUSY"
    assert "workdir is busy" in message
