"""Deterministic Repo Ops MCP surface and local authorization tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp.server import MCPServer

from helpers import call_error, call_success, error_code, registry_for
from serverfs_mcp.agent_client import AgentBridgeRemoteError
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir, WorkdirRegistry


class FakeRepoClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {
            "repo.inspect": {
                "view": "status",
                "head": "abc123",
                "branch": "main",
                "upstream": "origin/main",
                "unstaged_paths": [],
                "staged_paths": [],
                "untracked_paths": [],
            },
            "repo.action": {
                "action": "wiki_lint",
                "mutates": False,
                "returncode": 0,
                "stdout": "ok\n",
                "stderr": "",
            },
            "repo.commit": {"commit": "abc123", "paths": ["wiki/topic.md"], "summary": "ok"},
            "repo.push": {"pushed": True, "output": ""},
        }
        self.error: AgentBridgeRemoteError | None = None

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.responses[method]


def repo_workdir(
    tmp_path: Path,
    *,
    enabled: bool = True,
    read_only: bool = False,
    allow_hidden: bool = False,
) -> Workdir:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    return Workdir(
        alias="repo",
        root=root,
        description="Repo Ops test workdir",
        read_only=read_only,
        policy=EffectiveWorkdirPolicy(
            allow_hidden=allow_hidden,
            repo_ops_enabled=enabled,
        ),
    )


def server_with_repo_client(
    tmp_path: Path,
    client: FakeRepoClient,
    *,
    workdirs: list[Workdir] | None = None,
) -> MCPServer:
    wds = workdirs if workdirs is not None else [repo_workdir(tmp_path)]
    settings = Settings(
        agent_bridge_enabled=True,
        agent_bridge_socket="/tmp/test-host-bridge.sock",
        agent_lock_dir=str(tmp_path / "locks"),
    )
    return create_server(settings, registry_for(*wds), client)  # type: ignore[arg-type]


def tool_names(server: MCPServer) -> set[str]:
    async def _get() -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    return asyncio.run(_get())


def test_repo_ops_disabled_keeps_base_surface(tmp_path: Path) -> None:
    wd = repo_workdir(tmp_path, enabled=False)
    server = create_server(Settings(), WorkdirRegistry([wd]))
    names = tool_names(server)
    assert len(names) == 11
    assert "inspect_repo" not in names
    assert "submit_agent_task" not in names


def test_repo_ops_requires_host_bridge(tmp_path: Path) -> None:
    wd = repo_workdir(tmp_path)
    with pytest.raises(ValueError, match="host Bridge"):
        create_server(Settings(), WorkdirRegistry([wd]))


def test_repo_ops_only_surface_adds_exactly_four_tools_without_agent_tools(tmp_path: Path) -> None:
    client = FakeRepoClient()
    server = server_with_repo_client(tmp_path, client)
    names = tool_names(server)
    assert {"inspect_repo", "run_repo_action", "commit_repo_changes", "push_repo"} <= names
    assert "submit_agent_task" not in names
    assert "list_agent_runtimes" not in names
    assert len(names) == 15


def test_list_workdirs_exposes_repo_ops_capability(tmp_path: Path) -> None:
    client = FakeRepoClient()
    server = server_with_repo_client(tmp_path, client)
    result = call_success(server, "list_workdirs", {})
    assert result["workdirs"][0]["repo_ops"] is True
    assert result["workdirs"][0]["agent_mode"] == "disabled"


def test_repo_tool_schemas_expose_no_generic_command_or_git_escape_hatches(tmp_path: Path) -> None:
    client = FakeRepoClient()
    server = server_with_repo_client(tmp_path, client)

    async def _tools():
        return {tool.name: tool for tool in await server.list_tools()}

    tools = asyncio.run(_tools())
    assert set(tools["inspect_repo"].input_schema["properties"]) == {"workdir", "view", "paths"}
    assert set(tools["run_repo_action"].input_schema["properties"]) == {
        "workdir",
        "action",
        "path",
        "release_tag",
    }
    assert set(tools["commit_repo_changes"].input_schema["properties"]) == {
        "workdir",
        "paths",
        "message",
    }
    assert set(tools["push_repo"].input_schema["properties"]) == {"workdir"}

    forbidden = {"command", "executable", "argv", "env", "cwd", "remote", "ref", "force", "shell"}
    for name in ("inspect_repo", "run_repo_action", "commit_repo_changes", "push_repo"):
        assert forbidden.isdisjoint(tools[name].input_schema["properties"])


def test_inspect_repo_maps_to_repo_rpc_without_agent_submission(tmp_path: Path) -> None:
    client = FakeRepoClient()
    server = server_with_repo_client(tmp_path, client)
    result = call_success(server, "inspect_repo", {"workdir": "repo", "view": "status"})
    assert result["head"] == "abc123"
    assert result["upstream"] == "origin/main"
    assert result["dirty"] is False
    assert result["restricted_changes_present"] is False
    assert client.calls == [("repo.inspect", {"workdir": "repo", "view": "status", "paths": []})]
    assert all(method != "task.submit" for method, _ in client.calls)


def test_status_filters_denied_and_hidden_paths_from_bridge_response(tmp_path: Path) -> None:
    client = FakeRepoClient()
    client.responses["repo.inspect"] = {
        "view": "status",
        "head": "abc123",
        "branch": "main",
        "upstream": "origin/main",
        "unstaged_paths": ["wiki/topic.md", ".env"],
        "staged_paths": [],
        "untracked_paths": ["secret.pem", "notes.md"],
    }
    server = server_with_repo_client(tmp_path, client)
    result = call_success(server, "inspect_repo", {"workdir": "repo", "view": "status"})
    assert result["unstaged_paths"] == ["wiki/topic.md"]
    assert result["untracked_paths"] == ["notes.md"]
    assert result["dirty"] is True
    assert result["restricted_changes_present"] is True


def test_diff_requires_explicit_policy_validated_paths(tmp_path: Path) -> None:
    client = FakeRepoClient()
    server = server_with_repo_client(tmp_path, client)
    message = call_error(server, "inspect_repo", {"workdir": "repo", "view": "diff"})
    assert error_code(message) == "INVALID_REQUEST"
    assert client.calls == []


def test_diff_paths_are_policy_validated_before_bridge_rpc(tmp_path: Path) -> None:
    client = FakeRepoClient()
    wd = repo_workdir(tmp_path)
    server = server_with_repo_client(tmp_path, client, workdirs=[wd])

    message = call_error(
        server,
        "inspect_repo",
        {"workdir": "repo", "view": "diff", "paths": ["../outside.txt"]},
    )
    assert error_code(message) == "PATH_OUTSIDE_WORKDIR"
    assert client.calls == []

    message = call_error(
        server,
        "inspect_repo",
        {"workdir": "repo", "view": "diff", "paths": [".env"]},
    )
    assert error_code(message) in {"HIDDEN_PATH_NOT_ALLOWED", "DENIED_PATH"}
    assert client.calls == []


def test_named_action_passes_only_name_and_policy_valid_path(tmp_path: Path) -> None:
    client = FakeRepoClient()
    wd = repo_workdir(tmp_path)
    (wd.root / "raw.md").write_text("source", encoding="utf-8")
    server = server_with_repo_client(tmp_path, client, workdirs=[wd])

    call_success(
        server,
        "run_repo_action",
        {"workdir": "repo", "action": "registry_ensure", "path": "raw.md"},
    )
    assert client.calls[-1] == (
        "repo.action",
        {"workdir": "repo", "action": "registry_ensure", "path": "raw.md"},
    )


def test_actions_commit_and_push_require_read_write_workdir(tmp_path: Path) -> None:
    client = FakeRepoClient()
    wd = repo_workdir(tmp_path, read_only=True)
    server = server_with_repo_client(tmp_path, client, workdirs=[wd])

    action_error = call_error(
        server,
        "run_repo_action",
        {"workdir": "repo", "action": "wiki_lint"},
    )
    assert error_code(action_error) == "WORKDIR_READ_ONLY"
    commit_error = call_error(
        server,
        "commit_repo_changes",
        {"workdir": "repo", "paths": ["wiki/topic.md"], "message": "Update topic"},
    )
    assert error_code(commit_error) == "WORKDIR_READ_ONLY"
    push_error = call_error(server, "push_repo", {"workdir": "repo"})
    assert error_code(push_error) == "WORKDIR_READ_ONLY"
    assert client.calls == []


def test_commit_maps_exact_paths_and_push_has_no_remote_or_ref(tmp_path: Path) -> None:
    client = FakeRepoClient()
    server = server_with_repo_client(tmp_path, client)

    call_success(
        server,
        "commit_repo_changes",
        {"workdir": "repo", "paths": ["wiki/topic.md"], "message": "Update topic"},
    )
    call_success(server, "push_repo", {"workdir": "repo"})
    assert client.calls == [
        (
            "repo.commit",
            {"workdir": "repo", "paths": ["wiki/topic.md"], "message": "Update topic"},
        ),
        ("repo.push", {"workdir": "repo"}),
    ]


def test_repo_bridge_errors_keep_repo_ops_vocabulary(tmp_path: Path) -> None:
    client = FakeRepoClient()
    client.error = AgentBridgeRemoteError("REPO_OPS_DISABLED", "Repo Ops is disabled")
    server = server_with_repo_client(tmp_path, client)
    message = call_error(server, "inspect_repo", {"workdir": "repo", "view": "status"})
    assert error_code(message) == "REPO_OPS_DISABLED"


@pytest.mark.parametrize(
    "tag", ["../../xxx", "main", "refs/heads/main", "v1.2.3;id", "v1.2.3 --force"]
)
def test_release_tag_rejected_before_rpc(tmp_path: Path, tag: str) -> None:
    client = FakeRepoClient()
    server = server_with_repo_client(tmp_path, client)
    message = call_error(
        server, "run_repo_action", {"workdir": "repo", "action": "tag_release", "release_tag": tag}
    )
    assert error_code(message) == "INVALID_RELEASE_TAG"
    assert client.calls == []


@pytest.mark.parametrize(
    "extra", [{"force": True}, {"commit": "a" * 40}, {"workflow": "evil.yml"}, {"remote": "evil"}]
)
def test_release_extra_fields_are_rejected(tmp_path: Path, extra: dict) -> None:
    client = FakeRepoClient()
    server = server_with_repo_client(tmp_path, client)
    call_error(
        server,
        "run_repo_action",
        {"workdir": "repo", "action": "tag_release", "release_tag": "v1.1.0-beta.3", **extra},
    )
    assert client.calls == []


def test_structured_release_tag_reaches_repo_rpc_only(tmp_path: Path) -> None:
    client = FakeRepoClient()
    server = server_with_repo_client(tmp_path, client)
    call_success(
        server,
        "run_repo_action",
        {"workdir": "repo", "action": "tag_release", "release_tag": "v1.1.0-beta.3"},
    )
    assert client.calls == [
        (
            "repo.action",
            {
                "workdir": "repo",
                "action": "tag_release",
                "path": None,
                "release_tag": "v1.1.0-beta.3",
            },
        )
    ]
